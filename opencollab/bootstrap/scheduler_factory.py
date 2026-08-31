"""Scheduler factory for the high-level CLI and SDK entry points.

``build_scheduler`` loads the team config, wires every concrete dependency,
hands the ``Scheduler`` a ``LaunchSpec`` + ``Topology``, and lets it create
agent 0 (the init process). This is the one place that knows the full
collaborator graph for a multi-agent run.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.hooks import ShellHookRunner
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.hooks import HookEventSubscriber
from opencollab.application.scheduler import LaunchSpec, Scheduler
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_TOP_P,
    resolve_thinking_params,
)
from opencollab.bootstrap.container import RuntimeContext, build_workspace_safety_policy
from opencollab.bootstrap.context_builder import SpawnConfig
from opencollab.bootstrap.session_factory import (
    SESSION_MAX_STEPS,
    DefaultSessionFactory,
    agent_save_path,
    make_run_dir,
)
from opencollab.bootstrap.team_config import (
    TeamConfig,
    load_team_config,
    resolve_team_file,
)
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP

# The one tool that carries a message from one live agent to another
# (``adapters.tools.message.MessageAgentTool``). Named here rather than
# imported from the registry so the composition root states the dependency it
# actually has: a topology edge is walkable only if the source role holds this.
_MESSAGING_TOOL_NAME = "message_agent"


def _reject_unwalkable_edges(team: TeamConfig) -> None:
    """Refuse a prebuilt team whose declared edges no agent can walk.

    Only under ``prebuild_team``. There, ``spawn_agent`` is refused for the
    whole run (``SchedulerTeamMixin._refuse_spawn_when_prebuilt``), so
    ``message_agent`` is the only channel left between two seated agents. A role
    that is given an outgoing edge but not that tool is seated mute: the
    topology says it may address a teammate and nothing it can call will do so.
    The run still starts, still records that edge as ``assigned.topology_edges``,
    and produces a transcript in which the edge was simply never used — which
    reads as a finding about the model rather than about the config.

    Under the product default (``prebuild_team=False``) the same shape is
    legitimate and must not be touched: the built-in Analyst has two outgoing
    edges and no ``message_agent``, because it walks them with ``spawn_agent``
    and receives results back through the join path. That is why this is called
    from behind the switch and nowhere else.

    Raised before anything is wired, so the failure lands before agent 0 exists
    and before a single token is spent — the same bargain
    ``ensure_team_prebuilt`` already makes for a seat it cannot fill.
    """
    offenders: list[tuple[str, list[str], list[str]]] = []
    for source in sorted(team.topology.edges):
        destinations = sorted(team.topology.edges[source])
        role = team.roles.get(source)
        if not destinations or role is None:
            continue
        if _MESSAGING_TOOL_NAME in role.tools:
            continue
        offenders.append((source, destinations, sorted(role.tools)))
    if not offenders:
        return
    detail = "\n".join(
        f"  - role '{source}' may address {', '.join(destinations)} "
        f"but its tools are [{', '.join(tools)}]\n"
        f"    unwalkable edges: "
        + ", ".join(f"{source} -> {destination}" for destination in destinations)
        for source, destinations, tools in offenders
    )
    named = ", ".join(f"'{source}'" for source, _, _ in offenders)
    raise ValueError(
        "prebuild_team: this team declares edges that no agent can walk.\n"
        f"{detail}\n"
        f"A prebuilt team refuses spawn_agent, so '{_MESSAGING_TOOL_NAME}' is "
        "the only channel between seated agents. Add "
        f"'{_MESSAGING_TOOL_NAME}' to the `tools:` list of {named}, or remove "
        "those roles' entries from `topology:`."
    )



def build_scheduler(
    ctx: RuntimeContext,
    *,
    use_worktrees: bool,
    interactive: bool,
    session_file: str | None = None,
    auto_save: bool = True,
    enable_hooks: bool = True,
    team_config_path: str | os.PathLike[str] | None = None,
    resolved_team_config: TeamConfig | None = None,
    save_dir: str | os.PathLike[str] | None = None,
    allow_unisolated_child_tests: bool = False,
    prebuild_team: bool = False,
    allow_unisolated_shell: bool | None = None,
    max_steps: int = SESSION_MAX_STEPS,
    serialize_turns: bool = False,
    environment: Environment | None = None,
    record_delivery_tree: bool = False,
) -> Scheduler:
    """Build the Scheduler and let it create agent 0 (the init process).

    Loads an explicitly selected team config (or the built-in Self-Collaboration team),
    wires dependencies, and hands the scheduler a ``LaunchSpec``;
    ``scheduler.create_init_process`` builds agent 0 (aid=0) through the factory
    and applies the launch spec. ``session_file`` resumes agent 0's history;
    ``auto_save`` writes a structured-JSON transcript per agent
    (``agent_<aid>_<role>.json``) plus a ``team.json`` manifest under a
    timestamped run folder in ``<workspace>/.opencollab/sessions``.

    When ``enable_hooks`` and the team config declares ``hooks``, a
    ``HookEventSubscriber`` is attached to the team event bus so configured
    shell commands fire on lifecycle events. Disable (e.g. under eval) to keep
    runs free of hook side effects.

    ``prebuild_team`` switches the run to a static topology: every role the team
    config declares is seated before the first model call and ``spawn_agent`` is
    refused thereafter, so the roster is an input to the run rather than
    something the model decides mid-run. Off by default; the scheduler behaves
    exactly as before while it is off.

    ``environment`` is where this run works. Left unset every agent works on
    the host workspace; supplied, agent 0 works in it and each teammate gets an
    isolated view of the same place -- which is how a team reaches a repository
    that exists only inside a container.

    ``serialize_turns`` holds the team to one turn at a time: a teammate a
    message wakes waits for the running turn to finish instead of running beside
    it. It changes *when* an agent runs, never whether it may — the topology
    keeps every declared edge and ``message_agent`` stays voluntary, so whether
    the agents hand work to each other is still theirs to decide. Off by
    default, which is the concurrent behaviour.

    ``interactive`` and ``allow_unisolated_shell`` are two different facts about
    a run, and only one of them is about a human:

    * ``interactive`` — is there a person at this run to put a question to? It
      is what gives the entry role ``ask_user``, and nothing else here.
    * ``allow_unisolated_shell`` — may an agent seated at the start execute
      commands the OS does not sandbox? A worktree is not a sandbox, so this is
      what decides whether a seated agent's ``bash`` runs a command or refuses
      it.

    They coincided while the only run with a shell was a run with a human
    watching it, which is why one boolean used to answer both. An unattended
    batch run pulls them apart: its agents must be able to run ``git`` in their
    worktrees, and there is nobody for them to ask anything. Passing
    ``max_steps`` is the step ceiling every seat is built with — the entry agent
    and each teammate alike. It is a runaway guard rather than an allowance:
    tokens are the resource a run is held to, and steps are counted and
    reported. Before this parameter existed the ceiling could not be set at all
    on this path, and the two hard-coded defaults gave the entry agent twice
    what a teammate got.

    ``allow_unisolated_shell=True`` with ``interactive=False`` is exactly that
    run, and it is not expressible with one flag.

    ``record_delivery_tree`` wires a read-only probe over the tree this run is
    graded on -- agent 0's workspace, which is the environment when one is
    given -- and the scheduler then records that tree's diff at each seat
    boundary (``SchedulerDeliveryTreeMixin``). Off by default because it costs a
    ``git diff`` per boundary and answers a question only an experiment asks:
    which seat was working when a delivered line arrived. Off, no probe is
    built and no git command runs.

    ``None`` — the default — means "whatever ``interactive`` says", so every
    existing call site keeps its current behaviour without being touched.
    """
    if session_file is not None and not Path(session_file).is_file():
        raise ValueError(f"session file does not exist: {session_file}")

    cfg = ctx.config
    team_cfg = (
        resolved_team_config
        if resolved_team_config is not None
        else load_team_config(ctx.workspace, path=team_config_path)
    )
    if prebuild_team:
        _reject_unwalkable_edges(team_cfg)
    event_bus = EventBus(ctx.event_sink)

    # Per-run folder: every agent's transcript plus a team.json manifest land
    # here. Known before the factory so spawned children inherit the same dir.
    run_dir: str | None = None
    if auto_save:
        run_dir = (
            os.path.abspath(os.fspath(save_dir))
            if save_dir is not None
            else make_run_dir(ctx.workspace)
        )
    lead_save_path = agent_save_path(run_dir, 0, team_cfg.entry) if run_dir else None

    session_factory = DefaultSessionFactory(
        SpawnConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            wire_protocol=cfg.get("wire_protocol", "chat_completions"),
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            llm_timeout=cfg.get("llm_timeout", 600.0),
            temperature=cfg.get("temperature", DEFAULT_TEMPERATURE),
            top_p=cfg.get("top_p", DEFAULT_TOP_P),
            max_output_tokens=cfg.get(
                "max_output_tokens", DEFAULT_MAX_TOKENS_PER_STEP
            ),
            context_window=cfg.get("context_window"),
            thinking=cfg.get("thinking", DEFAULT_THINKING),
            thinking_params=resolve_thinking_params(cfg.get("thinking_params")),
            reasoning_effort=cfg.get("reasoning_effort"),
            llm_max_retries=cfg.get("llm_max_retries", 3),
            llm_connect_timeout=cfg.get("llm_connect_timeout", 30.0),
            llm_first_event_timeout=cfg.get("llm_first_event_timeout", 180.0),
            llm_stream_idle_timeout=cfg.get("llm_stream_idle_timeout", 180.0),
            llm_stream_chat=bool(cfg.get("llm_stream_chat", False)),
            provider_error_time_budget=cfg.get("provider_error_time_budget", 0.0),
            tracer=ctx.tracer,
            event_bus=event_bus,
            permission_policy=ctx.permission_policy,
            ask_policy=ctx.ask_policy,
            safety_policy_factory=build_workspace_safety_policy,
        ),
        team_cfg=team_cfg,
        lead_workspace=ctx.workspace,
        lead_environment=environment,
        interactive=interactive,
        save_dir=run_dir,
        allow_unisolated_child_tests=allow_unisolated_child_tests,
        # A prebuilt roster's teammates are declared nodes seated before the
        # first model call, so the factory gives them agent 0's shell instead of
        # the hardened default it gives a child a model spawned mid-run.
        prebuilt_roster=prebuild_team,
        allow_unisolated_shell=allow_unisolated_shell,
        max_steps=max_steps,
    )
    worktree_pool = WorktreePool(
        ctx.workspace,
        use_worktrees=use_worktrees,
        base_environment=environment,
    )

    # The graded tree is where agent 0 works: the environment when the run was
    # given one (a container the repository lives in), the host workspace
    # otherwise. Same choice ``_workflow_runtime_session`` makes for the probe
    # it hands a workflow, so both regimes measure the same directory.
    delivery_tree_probe = (
        EnvWorkingTreeProbe(
            environment if environment is not None else LocalEnvironment(ctx.workspace)
        )
        if record_delivery_tree
        else None
    )

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=worktree_pool,
        event_sink=event_bus,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        permission_policy=ctx.permission_policy,
        topology=team_cfg.topology,
        roles=tuple(team_cfg.roles),
        prebuild_team=prebuild_team,
        serialize_turns=serialize_turns,
        delivery_tree_probe=delivery_tree_probe,
    )

    # Attach hooks after the scheduler exists so the runner can hold its handle
    # (the coordination-ready seam for a future ``agent`` executor). Subscribing
    # appends to the same bus, leaving the TUI sink at target[0] untouched.
    if enable_hooks and team_cfg.hooks:
        runner = ShellHookRunner(
            team_cfg.hooks,
            scheduler=scheduler,
            workspace=ctx.workspace,
        )
        scheduler.register_lifecycle_resource(runner, description="hook processes")
        event_bus.subscribe(HookEventSubscriber(runner))

    # Persist a team.json manifest from the live roster on every roster change.
    # Wired before create_init_process so agent 0 is captured on registration.
    if run_dir is not None:
        store = SessionStore()
        manifest_path = os.path.join(run_dir, "team.json")
        team_file = (
            Path(os.path.abspath(os.fspath(team_config_path)))
            if team_config_path is not None
            else resolve_team_file(ctx.workspace)
        )
        run_id = os.path.basename(run_dir)
        started_at = datetime.now(timezone.utc).isoformat()

        def _manifest_payload() -> dict:
            return {
                "run_id": run_id,
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "team_file": str(team_file) if team_file else None,
                "agents": scheduler.team_snapshot(),
            }

        def _write_manifest() -> None:
            store.save_manifest(manifest_path, _manifest_payload())

        def _prepare_manifest() -> Callable[[], None]:
            payload = _manifest_payload()
            return lambda: store.save_manifest(manifest_path, payload)

        scheduler.set_manifest_writer(
            _write_manifest,
            prepare_fn=_prepare_manifest,
        )

    scheduler.create_init_process(
        LaunchSpec(session_file=session_file, auto_save_path=lead_save_path)
    )
    return scheduler


__all__ = ["build_scheduler"]
