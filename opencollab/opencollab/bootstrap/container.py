"""Composition root for OpenCollab.

Single file that knows how to wire every concrete adapter into the
application use cases. The CLI entry point (agent 0) and the eval harness
build their session/scheduler objects through the factory functions exposed
here.

Contents (top to bottom):
- ``RuntimeContext`` + ``build_runtime_context``: per-invocation context
  (workspace, config, tracer, UI hooks).
- ``build_workspace_safety_policy``: derives a sandbox interceptor from
  an Environment instance.
- ``TOOL_REGISTRY`` + ``build_tools_for_role`` + ``build_default_tools``:
  resolve tool *names* (from the team config) to concrete Tool instances.
- ``build_session_runtime``: the actual collaborator construction order
  (event bus, state, store, autosave, LLM, tool execution, compactor,
  run-loop use case).
- ``build_session`` / ``snapshot_session``: self-wiring application
  ``Session`` factories.
- ``ContextBuilder``: turns a role name into an ``Agent`` (topology-aware
  system prompt + resolved tools), shared by the lead and spawned agents.
- ``SpawnConfig`` + ``build_spawn_session`` + ``DefaultSessionFactory``:
  spawn-session wiring used by the scheduler.
- ``build_scheduler``: the high-level CLI entry point — loads the team config,
  builds agent 0 (the lead), and the Scheduler that runs and spawns children.
"""

from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.hooks import ShellHookRunner
from opencollab.adapters.llm import LLMClient, estimate_messages_tokens
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.edit import ApplyPatchTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_status import GitDiffTool
from opencollab.adapters.tools.human import AskUserTool
from opencollab.adapters.tools.message import MessageAgentTool, TeamStatusTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.tools.spawn import SpawnAgentTool, SpawnWithReviewTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.compaction import (
    DEFAULT_COMPACTION_THRESHOLD,
    ContextCompactionUseCase,
)
from opencollab.application.event_bus import EventBus
from opencollab.application.hooks import HookEventSubscriber
from opencollab.application.ports import (
    EventPublisherPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyFactory,
    SafetyPolicyPort,
    SessionStorePort,
    TracePort,
)
from opencollab.application.scheduler import LaunchSpec, Scheduler
from opencollab.application.session import Session, SessionRuntime
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.bootstrap.team_config import (
    RoleConfig,
    TeamConfig,
    default_team_config,
    load_team_config,
    resolve_team_file,
)
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------


@dataclass
class RuntimeContext:
    workspace: str
    config: dict
    tracer: Tracer | None
    event_sink: EventPublisherPort | None
    permission_policy: PermissionPort | None


def build_runtime_context(
    workspace: str,
    cli_overrides: dict,
    *,
    trace: bool,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext:
    abs_workspace = os.path.abspath(workspace)
    tracer = (
        Tracer(run_id=f"{run_id_prefix}{uuid.uuid4().hex[:8]}") if trace else None
    )

    return RuntimeContext(
        workspace=abs_workspace,
        config=dict(cli_overrides),
        tracer=tracer,
        event_sink=event_sink,
        permission_policy=permission_policy,
    )


# ---------------------------------------------------------------------------
# Safety + tools
# ---------------------------------------------------------------------------


def build_workspace_safety_policy(env: Any) -> SafetyPolicyPort | None:
    if env is None or not getattr(env, "workspace", None):
        return None
    return SandboxInterceptor(env.workspace)


# Tool name -> factory. Stateless tools need nothing; scheduler-bound tools take
# the scheduler so an agent can spawn/message via the SchedulerPort.
STATELESS_TOOL_FACTORIES: dict[str, Callable[[], Tool]] = {
    "bash": BashTool,
    "file_read": FileReadTool,
    "file_write": FileWriteTool,
    "apply_patch": ApplyPatchTool,
    "run_tests": RunTestsTool,
    "git_diff": GitDiffTool,
    "grep": GrepTool,
    "ask_user": AskUserTool,
}
SCHEDULER_TOOL_FACTORIES: dict[str, Callable[[Any], Tool]] = {
    "spawn_agent": SpawnAgentTool,
    "spawn_with_review": SpawnWithReviewTool,
    "message_agent": MessageAgentTool,
    "team_status": TeamStatusTool,
}
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(STATELESS_TOOL_FACTORIES) | frozenset(SCHEDULER_TOOL_FACTORIES)
# Tools that let a role act on teammates — used to decide whether to render the
# topology-aware "Your team" prompt section.
COORDINATION_TOOL_NAMES: frozenset[str] = frozenset(SCHEDULER_TOOL_FACTORIES)


def build_tools_for_role(
    tool_names: list[str],
    *,
    scheduler: Any = None,
    interactive: bool = False,
) -> list[Tool]:
    """Resolve tool names to Tool instances.

    ``ask_user`` is dropped in non-interactive (headless) mode. Scheduler-bound
    tools require a ``scheduler``. Unknown names raise — fail fast at startup.
    """
    tools: list[Tool] = []
    for name in tool_names:
        if name == "ask_user" and not interactive:
            continue
        if name in STATELESS_TOOL_FACTORIES:
            tools.append(STATELESS_TOOL_FACTORIES[name]())
        elif name in SCHEDULER_TOOL_FACTORIES:
            if scheduler is None:
                raise ValueError(
                    f"Tool '{name}' requires a scheduler but none was provided."
                )
            tools.append(SCHEDULER_TOOL_FACTORIES[name](scheduler))
        else:
            raise ValueError(
                f"Unknown tool '{name}' in team config. "
                f"Known tools: {sorted(KNOWN_TOOL_NAMES)}"
            )
    return tools


def build_default_tools(*, include_ask_user: bool = False) -> list[Tool]:
    """Canonical tool bundle: bash, file_read, file_write, grep, [ask_user]."""
    tools: list[Tool] = [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        GrepTool(),
    ]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools


# ---------------------------------------------------------------------------
# Session runtime construction
# ---------------------------------------------------------------------------


def _build_initial_state(agent: Agent) -> SessionState:
    return SessionState(messages=[{"role": "system", "content": agent.system_prompt}])


def build_session_runtime(
    *,
    agent: Agent,
    env: Environment | None = None,
    tracer: TracePort | None = None,
    max_budget_tokens: int = 200_000,
    max_steps: int = 100,
    compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    auto_save_path: str | None = None,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    llm_timeout: float = 600.0,
    store: SessionStorePort | None = None,
    auto_save_callback: Callable[[], None] | None = None,
    aid: int = -1,
) -> SessionRuntime:
    """Build a ``SessionRuntime`` with the same construction order
    ``Session.__init__`` used to perform inline.

    ``auto_save_callback`` is the bound method the facade exposes for
    autosave; we accept it as an argument so the runtime does not need to
    know about the facade.
    """
    resolved_env = env if env is not None else LocalEnvironment()
    resolved_store: SessionStorePort = store if store is not None else SessionStore()

    event_bus = EventBus(event_sink)
    if auto_save_path and auto_save_callback is not None:
        event_bus.subscribe(AutoSaveSubscriber(auto_save_callback))

    state = _build_initial_state(agent)
    state.aid = aid

    resolved_llm: LLMPort
    if llm is not None:
        resolved_llm = llm
    else:
        resolved_llm = LLMClient(
            model=agent.model,
            api_key=agent.api_key,
            base_url=agent.base_url,
            provider=agent.provider,
            request_timeout=llm_timeout,
        )

    tool_execution = ToolExecutionUseCase(
        agent=agent,
        environment=resolved_env,
        state=state,
        event_publisher=event_bus,
        tracer=tracer,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
    )
    compactor = ContextCompactionUseCase(
        state=state,
        llm=resolved_llm,
        event_publisher=event_bus,
        estimate_tokens=estimate_messages_tokens,
        tracer=tracer,
        compaction_threshold=compaction_threshold,
    )
    runner = SessionRunUseCase(
        agent=agent,
        state=state,
        llm=resolved_llm,
        event_publisher=event_bus,
        tool_execution=tool_execution,
        compaction=compactor,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
    )

    return SessionRuntime(
        state=state,
        event_bus=event_bus,
        llm=resolved_llm,
        store=resolved_store,
        tool_execution=tool_execution,
        compactor=compactor,
        runner=runner,
        auto_save_path=auto_save_path,
    )


# ---------------------------------------------------------------------------
# Session factory + snapshot
# ---------------------------------------------------------------------------


def build_session(
    *,
    agent: Agent,
    env: Environment | None = None,
    tracer: Tracer | None = None,
    max_budget_tokens: int = 200_000,
    max_steps: int = 100,
    compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    auto_save_path: str | None = None,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    llm_timeout: float = 600.0,
    store: SessionStorePort | None = None,
    aid: int = -1,
) -> Session:
    """Self-wiring ``Session`` factory.

    Callers that want full control over collaborators can still build a
    ``SessionRuntime`` via ``build_session_runtime`` and pass it directly
    to ``application.session.Session``.
    """
    session = Session.__new__(Session)
    runtime = build_session_runtime(
        agent=agent,
        env=env,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
        compaction_threshold=compaction_threshold,
        auto_save_path=auto_save_path,
        event_sink=event_sink,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
        llm=llm,
        llm_timeout=llm_timeout,
        store=store,
        auto_save_callback=session._auto_save,
        aid=aid,
    )
    Session.__init__(
        session,
        agent=agent,
        runtime=runtime,
        env=env,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
        compaction_threshold=compaction_threshold,
        auto_save_path=auto_save_path,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
    )
    return session


def load_session(
    path: str,
    agent: Agent,
    **kwargs: Any,
) -> Session:
    """Build a session and replace its messages with the JSONL at ``path``."""
    session = build_session(agent=agent, **kwargs)
    session.messages = session.store.load_messages(path, agent.system_prompt)
    return session


def snapshot_session(session: Session) -> Session:
    """Build an independent ``Session`` sharing the source's state.

    Drops the original session's internal ``AutoSaveSubscriber`` but
    re-attaches any external subscriber on the bus so external observers
    keep seeing events.
    """
    external_sink: EventPublisherPort | None = None
    for target in session.event_bus._targets:
        if not isinstance(target, AutoSaveSubscriber):
            external_sink = target  # type: ignore[assignment]
            break
    new = build_session(
        agent=session.agent,
        env=session.env,
        tracer=session.tracer,
        max_budget_tokens=session.max_budget_tokens,
        max_steps=session.max_steps,
        compaction_threshold=session.compaction_threshold,
        event_sink=external_sink,
        permission_policy=session.permission_policy,
        safety_policy=session._safety_policy,
    )
    new.messages = copy.deepcopy(session.messages)
    new.used_tokens = session.used_tokens
    new.step_count = session.step_count
    return new


# ---------------------------------------------------------------------------
# Spawn session wiring
# ---------------------------------------------------------------------------


def agent_save_path(save_dir: str, aid: int, role: str) -> str:
    """Per-agent transcript path within a run folder: ``agent_<aid>_<role>.json``."""
    return os.path.join(save_dir, f"agent_{aid}_{role}.json")


def make_run_dir(workspace: str) -> str:
    """A timestamped run folder under ``<workspace>/.opencollab/sessions``.

    A 4-char suffix is appended if a same-second folder already exists, so two
    runs started within the same second do not collide.
    """
    base = os.path.join(workspace, ".opencollab", "sessions")
    run_dir = os.path.join(base, datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))
    if os.path.exists(run_dir):
        run_dir = f"{run_dir}-{uuid.uuid4().hex[:4]}"
    return run_dir


@dataclass
class SpawnConfig:
    """Shared LLM/runtime config inherited by every spawned agent session."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    llm_timeout: float
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPort | None
    safety_policy_factory: SafetyPolicyFactory | None = None


class ContextBuilder:
    """Turns a role name into a ready-to-run ``Agent``.

    Owns the single role -> ``Agent`` assembly used for both the lead and
    spawned agents: it composes the system prompt (the role's prompt plus an
    auto-generated, topology-aware "Your team" section) and resolves the role's
    tool *names* to concrete Tool instances via the registry. Tool descriptions
    are NOT injected into the prompt — those already reach the LLM as
    function-calling schemas.
    """

    def __init__(self, team_cfg: TeamConfig, cfg: SpawnConfig):
        self._team = team_cfg
        self._cfg = cfg

    def build_agent(
        self,
        role_name: str,
        *,
        scheduler: Any = None,
        interactive: bool = False,
    ) -> Agent:
        role = self._team.role_for(role_name)
        system_prompt = self._compose_prompt(role_name, role)
        tools = build_tools_for_role(
            role.tools, scheduler=scheduler, interactive=interactive
        )
        cfg = self._cfg
        return Agent(
            name=role_name,
            system_prompt=system_prompt,
            tools=tools,
            model=role.model or cfg.model,
            provider=cfg.provider,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )

    def _compose_prompt(self, role_name: str, role: RoleConfig) -> str:
        section = self._team_section(role_name, role)
        return f"{role.prompt}\n\n{section}" if section else role.prompt

    def _team_section(self, role_name: str, role: RoleConfig) -> str:
        topo = self._team.topology
        # The permissive default carries generic guidance in the base prompt;
        # the dynamic section only adds value for an explicit topology graph.
        if topo.allow_all:
            return ""
        targets = sorted(topo.edges.get(role_name, frozenset()))
        if not targets or not (set(role.tools) & COORDINATION_TOOL_NAMES):
            return ""
        lines = ["## Your team", "", "Roles you may spawn or message:"]
        lines += [f"- {t}" for t in targets]
        if {"team_status", "message_agent"} & set(role.tools):
            lines += [
                "",
                "Use `team_status` to list live agents (with their ids) and "
                "`message_agent` to send an async message to one.",
            ]
        return "\n".join(lines)


def build_spawn_session(
    *,
    role: str,
    env: Environment,
    cfg: SpawnConfig,
    budget: int,
    max_steps: int = 50,
    aid: int = -1,
    scheduler: Any = None,
    team_cfg: TeamConfig | None = None,
) -> Session:
    """Build the Agent + Session bundle for a spawned child agent.

    ``team_cfg`` defaults to the lead-only default team, so roles resolve to the
    generic spec (base tools). The safety policy is derived from the child's
    environment.
    """
    safety_policy = (
        cfg.safety_policy_factory(env)
        if cfg.safety_policy_factory is not None
        else None
    )
    builder = ContextBuilder(team_cfg or default_team_config(), cfg)
    agent = builder.build_agent(role, scheduler=scheduler, interactive=False)
    return build_session(
        agent=agent,
        env=env,
        tracer=cfg.tracer,
        max_budget_tokens=budget,
        max_steps=max_steps,
        event_sink=cfg.event_bus,
        permission_policy=cfg.permission_policy,
        safety_policy=safety_policy,
        llm_timeout=cfg.llm_timeout,
        aid=aid,
    )


class DefaultSessionFactory:
    """Default ``SessionFactoryPort`` implementation used by the scheduler.

    Holds the shared ``SpawnConfig`` (LLM config inherited by every session),
    the resolved ``TeamConfig`` (role prompts/tools + topology), and the
    lead-only composition bits (``lead_workspace`` for the local environment and
    ``interactive`` for the ask-user tool). All role -> Agent assembly is
    delegated to a single ``ContextBuilder``.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        team_cfg: TeamConfig | None = None,
        lead_workspace: str | None = None,
        interactive: bool = False,
        save_dir: str | None = None,
    ):
        self._cfg = cfg
        self._team = team_cfg or default_team_config()
        self._context_builder = ContextBuilder(self._team, cfg)
        self._lead_workspace = lead_workspace
        self._interactive = interactive
        # Run folder where every agent's transcript is persisted. When set,
        # spawned children get their own ``agent_<aid>_<role>.json`` autosave.
        self._save_dir = save_dir

    def build_spawn_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
        scheduler: Any = None,
    ) -> Session:
        cfg = self._cfg
        safety_policy = (
            cfg.safety_policy_factory(env)
            if cfg.safety_policy_factory is not None
            else None
        )
        agent = self._context_builder.build_agent(
            role, scheduler=scheduler, interactive=False
        )
        auto_save_path = (
            agent_save_path(self._save_dir, aid, role)
            if self._save_dir
            else None
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            max_steps=max_steps,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=safety_policy,
            auto_save_path=auto_save_path,
            llm_timeout=cfg.llm_timeout,
            aid=aid,
        )

    def create_lead_session(
        self,
        *,
        scheduler: Any,
        launch: LaunchSpec,
        budget: int,
        aid: int = 0,
    ) -> Session:
        """Build agent 0 with its local env, lead-role tools, and prompt.

        The scheduler-bound tools are resolved against ``scheduler`` here, so the
        lead<->scheduler cycle is closed inside this single handshake.
        ``launch.auto_save_path`` wires the auto-save subscriber; resume/seed is
        left to ``Session.apply_launch``.
        """
        cfg = self._cfg
        env = LocalEnvironment(self._lead_workspace)
        agent = self._context_builder.build_agent(
            "lead", scheduler=scheduler, interactive=self._interactive
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=build_workspace_safety_policy(env),
            auto_save_path=launch.auto_save_path,
            llm_timeout=cfg.llm_timeout,
            aid=aid,
        )


# ---------------------------------------------------------------------------
# Scheduler factory (the CLI entry point)
# ---------------------------------------------------------------------------


def build_scheduler(
    ctx: RuntimeContext,
    *,
    use_worktrees: bool,
    interactive: bool,
    session_file: str | None = None,
    auto_save: bool = True,
    enable_hooks: bool = True,
) -> Scheduler:
    """Build the Scheduler and let it create agent 0 (the init process).

    Loads the team config (roles/tools/topology) from the workspace, wires
    dependencies, and hands the scheduler a ``LaunchSpec``;
    ``scheduler.create_init_process`` builds agent 0 (aid=0) through the factory
    and applies the launch spec. ``session_file`` resumes agent 0's history;
    ``auto_save`` writes a structured-JSON transcript per agent
    (``agent_<aid>_<role>.json``) plus a ``team.json`` manifest under a
    timestamped run folder in ``<workspace>/.opencollab/sessions``.

    When ``enable_hooks`` and the team config declares ``hooks``, a
    ``HookEventSubscriber`` is attached to the team event bus so configured
    shell commands fire on lifecycle events. Disable (e.g. under eval) to keep
    runs free of hook side effects.
    """
    cfg = ctx.config
    team_cfg = load_team_config(ctx.workspace)
    event_bus = EventBus(ctx.event_sink)

    # Per-run folder: every agent's transcript plus a team.json manifest land
    # here. Known before the factory so spawned children inherit the same dir.
    run_dir: str | None = make_run_dir(ctx.workspace) if auto_save else None
    lead_save_path = agent_save_path(run_dir, 0, "lead") if run_dir else None

    session_factory = DefaultSessionFactory(
        SpawnConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            llm_timeout=cfg.get("llm_timeout", 600.0),
            tracer=ctx.tracer,
            event_bus=event_bus,
            permission_policy=ctx.permission_policy,
            safety_policy_factory=build_workspace_safety_policy,
        ),
        team_cfg=team_cfg,
        lead_workspace=ctx.workspace,
        interactive=interactive,
        save_dir=run_dir,
    )
    worktree_pool = WorktreePool(ctx.workspace, use_worktrees=use_worktrees)

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=worktree_pool,
        event_sink=event_bus,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        permission_policy=ctx.permission_policy,
        topology=team_cfg.topology,
        roles=tuple(team_cfg.roles),
    )

    # Attach hooks after the scheduler exists so the runner can hold its handle
    # (the coordination-ready seam for a future ``agent`` executor). Subscribing
    # appends to the same bus, leaving the TUI sink at target[0] untouched.
    if enable_hooks and team_cfg.hooks:
        runner = ShellHookRunner(team_cfg.hooks, scheduler=scheduler)
        event_bus.subscribe(HookEventSubscriber(runner))

    # Persist a team.json manifest from the live roster on every roster change.
    # Wired before create_init_process so agent 0 is captured on registration.
    if run_dir is not None:
        store = SessionStore()
        manifest_path = os.path.join(run_dir, "team.json")
        team_file = resolve_team_file(ctx.workspace)
        run_id = os.path.basename(run_dir)
        started_at = datetime.now(timezone.utc).isoformat()

        def _write_manifest() -> None:
            store.save_manifest(manifest_path, {
                "run_id": run_id,
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "team_file": str(team_file) if team_file else None,
                "agents": scheduler.team_snapshot(),
            })

        scheduler.set_manifest_writer(_write_manifest)

    scheduler.create_init_process(
        LaunchSpec(session_file=session_file, auto_save_path=lead_save_path)
    )
    return scheduler


__all__ = [
    "ContextBuilder",
    "DefaultSessionFactory",
    "RuntimeContext",
    "SessionRuntime",
    "SpawnConfig",
    "agent_save_path",
    "make_run_dir",
    "build_default_tools",
    "build_runtime_context",
    "build_session",
    "build_session_runtime",
    "build_scheduler",
    "build_spawn_session",
    "build_tools_for_role",
    "build_workspace_safety_policy",
    "load_session",
    "snapshot_session",
]
