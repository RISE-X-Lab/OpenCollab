"""Spawn-session wiring used by the scheduler.

``build_session`` is the self-wiring ``Session`` factory (with its
``load_session``/``snapshot_session`` companions); ``build_spawn_session`` and
``DefaultSessionFactory`` assemble the Agent + Session bundle for spawned
children and for agent 0 (the lead), delegating role -> Agent assembly to a
``ContextBuilder``. The ``SessionRuntime`` core stays in ``container``
(``build_session_runtime`` — the only place ``LLMClient`` is instantiated).
"""

from __future__ import annotations

import copy
import inspect
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.llm.providers import RESPONSES
from opencollab.adapters.llm.retry import RetryTimeBudget
from opencollab.adapters.llm.types import model_capabilities
from opencollab.adapters.repo_map import build_repo_map
from opencollab.adapters.safe_files import ensure_directory_no_symlinks
from opencollab.adapters.trace import Tracer
from opencollab.application.ports import (
    AskUserPort,
    EventPublisherPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SchedulerPort,
    SessionStorePort,
    ShaperPort,
)
from opencollab.application.scheduler import LaunchSpec
from opencollab.application.session import Session
from opencollab.bootstrap.container import build_session_runtime, build_skill_store
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.runtime_context import build_workspace_safety_policy
from opencollab.bootstrap.team_config import (
    BASE_TOOL_NAMES,
    TeamConfig,
    default_team_config,
)
from opencollab.domain.agent import Agent
from opencollab.domain.identity import role_storage_slug, validate_role_identity


class SnapshotSessionError(RuntimeError):
    """An independent session snapshot could not be created safely."""


def validate_responses_model_controls(
    model: str | None,
    *,
    role_name: str,
    tools_present: bool = False,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Reject known-incompatible Responses controls before side effects.

    Capability metadata is deliberately dimension-specific: unknown models keep
    neutral tool/streaming defaults, while known reasoning families fail closed
    for sampling and reasoning controls.  This helper is shared by team and
    workflow bootstrap paths; the provider adapter remains the final runtime
    guard for dynamically assembled requests.
    """
    capabilities = model_capabilities(model)
    if tools_present and not capabilities.supports_responses_tools:
        raise ValueError(
            f"role {role_name!r} model {model!r} does not support Responses tools"
        )
    if top_p is not None and not capabilities.supports_responses_sampling:
        raise ValueError(
            f"role {role_name!r} model {model!r} does not support Responses sampling"
        )
    if reasoning_effort is not None and not capabilities.supports_responses_reasoning:
        raise ValueError(
            f"role {role_name!r} model {model!r} does not support Responses reasoning_effort"
        )


def _team_budget_guard(scheduler: SchedulerPort | None) -> Callable[[], bool] | None:
    """A zero-arg predicate over the scheduler's aggregate budget, or ``None``.

    Threaded into the session runner so its precheck can enforce the team-wide
    ceiling without the application layer importing the concrete Scheduler.
    """
    if scheduler is None:
        return None
    return lambda: scheduler.budget_exhausted


def agent_save_path(save_dir: str, aid: int, role: str) -> str:
    """Per-agent transcript path within a run folder: ``agent_<aid>_<role>.json``."""
    if isinstance(aid, bool) or not isinstance(aid, int) or aid < 0:
        raise ValueError("agent id must be a non-negative integer")
    role_component = role_storage_slug(role)
    base = os.path.abspath(save_dir)
    path = os.path.abspath(
        os.path.join(base, f"agent_{aid}_{role_component}.json")
    )
    if os.path.commonpath((base, path)) != base:
        raise ValueError("agent transcript path escapes run directory")
    return path


# A workflow run folder groups one workflow's per-role conversation transcripts
# (``<seq>_<role>.json``) the way a team run folder groups ``agent_<aid>_<role>``
# transcripts. Its orchestration signals (phases, logs, step metrics) go to one
# ``orchestration.jsonl``; a ``workflow.json`` manifest ties the folder together.
ORCHESTRATION_FILENAME = "orchestration.jsonl"
WORKFLOW_MANIFEST_FILENAME = "workflow.json"

# Prefix on a workflow run folder's name so ``ls .opencollab/sessions/`` tells a
# workflow run (``wf-<timestamp>``) apart from a team run (``<timestamp>``) at a
# glance — both share the same parent dir. The manifest filename
# (``workflow.json`` vs ``team.json``) is the in-folder discriminator.
WORKFLOW_RUN_PREFIX = "wf-"

# Longest slug kept from an agent label when naming its transcript file.
_MAX_LABEL_SLUG_LEN = 40


def slug_label(label: str | None) -> str:
    """Filename-safe slug from an agent label (``coder:s1r2`` -> ``coder-s1r2``).

    Collapses any run of characters outside ``[A-Za-z0-9._-]`` to a single dash,
    trims separators, and caps length so a label can never produce an unsafe or
    unbounded filename. Empty / falsy labels yield ``""`` (caller omits the
    suffix entirely).
    """
    if not label:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")
    return cleaned[:_MAX_LABEL_SLUG_LEN]


def workflow_transcript_path(save_dir: str, seq: int, label: str | None) -> str:
    """Per-session transcript path within a workflow run folder.

    ``<save_dir>/<seq>_<role>.json`` (e.g. ``000_analyst.json``,
    ``001_coder-p1r2.json``), mirroring how a team run folder names agents
    ``agent_<aid>_<role>.json``. The ``seq`` prefix orders sessions by creation
    and guarantees uniqueness when one role runs more than once; the slugged
    ``label`` carries the role so a run folder reads as its phases at a glance.
    A labelless session falls back to ``<seq>.json``.
    """
    slug = slug_label(label)
    stem = f"{seq:03d}_{slug}" if slug else f"{seq:03d}"
    return os.path.join(save_dir, f"{stem}.json")


def make_run_dir(workspace: str, *, prefix: str = "") -> str:
    """A timestamped run folder under ``<workspace>/.opencollab/sessions``.

    The folder is named ``<prefix><timestamp>``; teams pass no prefix while
    workflow runs pass ``WORKFLOW_RUN_PREFIX`` so the two are distinguishable at
    a glance even though they share this parent dir. A random suffix lets
    concurrent callers reserve their directories without a retry loop.
    """
    if (
        not isinstance(prefix, str)
        or "/" in prefix
        or "\\" in prefix
        or any(ord(character) < 32 or ord(character) == 127 for character in prefix)
        or len(prefix.encode("utf-8", errors="surrogatepass")) > 32
    ):
        raise ValueError("run directory prefix must be one short safe component")
    try:
        workspace_root = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"workspace is not a real directory: {workspace}") from exc
    if not workspace_root.is_dir():
        raise ValueError(f"workspace is not a real directory: {workspace}")
    base = workspace_root / ".opencollab" / "sessions"
    try:
        ensure_directory_no_symlinks(base)
    except OSError as exc:
        raise ValueError(
            f"workspace state parent is not a real directory: {workspace}"
        ) from exc
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = base / f"{prefix}{stamp}-{uuid.uuid4().hex}"
    run_dir.mkdir(mode=0o700)
    return str(run_dir)


def build_session(
    *,
    agent: Agent,
    env: Environment | None = None,
    tracer: Tracer | None = None,
    max_budget_tokens: int = 1_000_000,
    max_steps: int = 100,
    auto_save_path: str | None = None,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    ask_policy: AskUserPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    llm_timeout: float = 600.0,
    provider_retry_budget: RetryTimeBudget | None = None,
    store: SessionStorePort | None = None,
    aid: int = -1,
    seed_user_messages: list[dict[str, Any]] | None = None,
    seed_system_messages: list[dict[str, Any]] | None = None,
    shaper: ShaperPort | None = None,
    team_budget_exhausted: Callable[[], bool] | None = None,
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
        auto_save_path=auto_save_path,
        event_sink=event_sink,
        permission_policy=permission_policy,
        ask_policy=ask_policy,
        safety_policy=safety_policy,
        llm=llm,
        llm_timeout=llm_timeout,
        provider_retry_budget=provider_retry_budget,
        store=store,
        auto_save_callback=session._auto_save,
        auto_save_prepare_callback=session._prepare_auto_save,
        aid=aid,
        seed_user_messages=seed_user_messages,
        seed_system_messages=seed_system_messages,
        shaper=shaper,
        team_budget_exhausted=team_budget_exhausted,
    )
    Session.__init__(
        session,
        agent=agent,
        runtime=runtime,
        env=env,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
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
    """Build a session and restore the snapshot at ``path``."""
    session = build_session(agent=agent, **kwargs)
    session.restore(path)
    return session


def snapshot_session(
    session: Session,
    *,
    event_sink: EventPublisherPort | None = None,
) -> Session:
    """Build an isolated state snapshot from a forkable session environment.

    Snapshots never inherit autosave or external event subscribers: callers who
    want observation must explicitly supply ``event_sink``. The environment must
    implement a synchronous ``fork_snapshot()`` that returns a distinct owner;
    silently sharing a workspace would make the resulting sessions unsafe to run
    concurrently.
    """
    agent = _clone_snapshot_component(session.agent, label="agent")
    environment = _fork_snapshot_environment(session.env)
    new = build_session(
        agent=agent,
        env=environment,
        max_budget_tokens=session.max_budget_tokens,
        max_steps=session.max_steps,
        event_sink=event_sink,
        permission_policy=_clone_snapshot_component(
            session.permission_policy,
            label="permission policy",
        ),
        ask_policy=_clone_snapshot_component(
            session.tool_execution.ask_policy,
            label="ask policy",
        ),
        safety_policy=_clone_snapshot_component(
            session.tool_execution.safety_policy,
            label="safety policy",
        ),
        llm=session._llm,
        aid=session.state.aid,
    )
    state = _clone_snapshot_component(session.state, label="session state")
    new.state = state
    new.runner.state = state
    new.tool_execution.state = state
    return new


def _clone_snapshot_component(value: Any, *, label: str) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception as exc:  # noqa: BLE001 - snapshot must not share mutable state
        raise SnapshotSessionError(f"cannot clone {label} for an independent snapshot") from exc


def _fork_snapshot_environment(environment: Any) -> Environment:
    fork = getattr(environment, "fork_snapshot", None)
    if not callable(fork):
        raise SnapshotSessionError(
            "independent session snapshots require environment.fork_snapshot()"
        )
    try:
        snapshot_environment = fork()
    except Exception as exc:  # noqa: BLE001 - preserve the fork boundary
        raise SnapshotSessionError("cannot fork environment for an independent snapshot") from exc
    if inspect.iscoroutine(snapshot_environment):
        snapshot_environment.close()
    if (
        inspect.isawaitable(snapshot_environment)
        or snapshot_environment is environment
        or not hasattr(snapshot_environment, "workspace")
    ):
        raise SnapshotSessionError(
            "environment.fork_snapshot() must synchronously return a distinct environment"
        )
    return snapshot_environment


def build_spawn_session(
    *,
    role: str,
    env: Environment,
    cfg: SpawnConfig,
    budget: int,
    max_steps: int = 50,
    aid: int = -1,
    scheduler: SchedulerPort | None = None,
    team_cfg: TeamConfig | None = None,
    task: str | None = None,
    context: str = "",
) -> Session:
    """Build the Agent + Session bundle for a spawned child agent.

    ``team_cfg`` defaults to the Self-Collaboration team, so roles resolve to the
    generic spec (base tools). The safety policy is derived from the child's
    environment. When ``task`` is given it is seeded as the agent's first
    user-context message (the TASK-layer source), so no separate
    ``add_user_message`` is needed. Delegates to ``DefaultSessionFactory`` so the
    spawn-session assembly lives in one place.
    """
    role = validate_role_identity(role)
    factory = DefaultSessionFactory(cfg, team_cfg=team_cfg)
    return factory.build_spawn_session(
        role=role,
        env=env,
        budget=budget,
        max_steps=max_steps,
        aid=aid,
        scheduler=scheduler,
        task=task,
        context=context,
    )


class DefaultSessionFactory:
    """Default ``SessionFactoryPort`` implementation used by the scheduler.

    Holds the shared ``SpawnConfig`` (LLM config inherited by every session),
    the resolved ``TeamConfig`` (role prompts/tools + topology), and the
    agent-0 composition bits (``lead_workspace`` for the local environment and
    ``interactive`` for the ask-user tool). All role -> Agent assembly is
    delegated to a single ``ContextBuilder``.

    ``prebuilt_roster`` says whether this run's roster is an input to the run
    (every agent declared in the team file and seated before the first model
    call) or an outcome of it (children the model spawns mid-run). It is the
    only thing that separates a teammate from an ad-hoc child here, and it is
    read exclusively by ``_unisolated_shell_allowed``.

    ``allow_unisolated_shell`` answers the shell question directly instead of
    letting ``interactive`` answer it by implication. ``None`` keeps the old
    coupling (a human at the run is what licensed an unsandboxed shell), so a
    caller that does not pass it gets exactly the previous behaviour.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        team_cfg: TeamConfig | None = None,
        lead_workspace: str | None = None,
        interactive: bool = False,
        save_dir: str | None = None,
        allow_unisolated_child_tests: bool = False,
        prebuilt_roster: bool = False,
        allow_unisolated_shell: bool | None = None,
    ):
        self._cfg = cfg
        self._provider_retry_budget = (
            RetryTimeBudget(cfg.provider_error_time_budget)
            if cfg.provider_error_time_budget > 0
            else None
        )
        self._team = team_cfg or default_team_config()
        self._interactive = interactive
        self._validate_responses_tool_support()
        self._lead_workspace = lead_workspace
        self._allow_unisolated_child_tests = allow_unisolated_child_tests
        self._prebuilt_roster = bool(prebuilt_roster)
        self._allow_unisolated_shell = (
            interactive if allow_unisolated_shell is None else bool(allow_unisolated_shell)
        )
        # Run folder where every agent's transcript is persisted. When set,
        # spawned children get their own ``agent_<aid>_<role>.json`` autosave.
        self._save_dir = save_dir

    def _validate_responses_tool_support(self) -> None:
        """Reject statically incompatible team roles before opening a workspace."""
        if self._cfg.wire_protocol != RESPONSES:
            return

        # A restricted topology may name an undeclared destination.  Such a
        # destination is still spawnable and ``TeamConfig.role_for`` resolves it
        # to the generic BASE_TOOL_NAMES role, so it must participate in the
        # same startup preflight as explicitly declared roles.
        reachable_roles = dict(self._team.roles)
        for source, destinations in self._team.topology.edges.items():
            reachable_roles.setdefault(source, self._team.role_for(source))
            for destination in destinations:
                reachable_roles.setdefault(
                    destination,
                    self._team.role_for(destination),
                )

        for role_name, role in reachable_roles.items():
            model = role.model or self._cfg.model
            tool_names = set(role.tools)
            if role_name != self._team.entry or not self._interactive:
                tool_names.discard("ask_user")
            validate_responses_model_controls(
                model,
                role_name=role_name,
                tools_present=bool(tool_names),
                top_p=self._cfg.top_p,
                reasoning_effort=self._cfg.reasoning_effort,
            )
        fallback_tool_names = set(BASE_TOOL_NAMES) - {"ask_user"}
        if self._team.topology.allow_all:
            capabilities = model_capabilities(self._cfg.model)
            if fallback_tool_names and not capabilities.supports_responses_tools:
                raise ValueError(
                    f"default model {self._cfg.model!r} does not support Responses "
                    "tools required by ad-hoc roles"
                )
            if self._cfg.top_p is not None and not capabilities.supports_responses_sampling:
                raise ValueError(
                    f"default model {self._cfg.model!r} does not support Responses "
                    "sampling required by ad-hoc roles"
                )
            if (
                self._cfg.reasoning_effort is not None
                and not capabilities.supports_responses_reasoning
            ):
                raise ValueError(
                    f"default model {self._cfg.model!r} does not support Responses "
                    "reasoning_effort required by ad-hoc roles"
                )

    def _unisolated_shell_allowed(self, *, seated_at_start: bool) -> bool:
        """May this agent run commands the OS does not sandbox?

        The one source every session in this run reads, so agent 0 and its
        teammates cannot end up on different answers. ``ask_user`` is decided
        separately (``interactive`` plus the entry-role rule) — the two used to
        be the same boolean, which is how teammates came to be seated without a
        working shell while agent 0 had one.

        ``seated_at_start`` is what distinguishes an agent the run was given
        from one the run invented. Agent 0 is seated at the start by
        definition, and so is every teammate of a prebuilt roster: both are
        nodes a human declared in the team file, running where agent 0 runs, so
        both get agent 0's answer. A child a model spawned mid-run is not
        declared anywhere, and keeps the hardened default — it must be handed an
        OS-sandboxed environment before it can run a command.

        The left half is ``allow_unisolated_shell``, which defaults to
        ``interactive`` and so reproduces the old rule unless a caller states
        otherwise. An unattended run that states it — a batch experiment whose
        agents must be able to run ``git`` — gets the shell without also
        acquiring a human it could put a question to.
        """
        return self._allow_unisolated_shell and seated_at_start

    def _fresh_context_builder(self) -> ContextBuilder:
        """Snapshot bounded workspace context at the new session's start."""
        skill_store = build_skill_store(self._lead_workspace)
        project_context = (
            build_repo_map(self._lead_workspace)
            if self._lead_workspace
            else None
        )
        return ContextBuilder(
            self._team,
            self._cfg,
            skill_store=skill_store,
            project_context=project_context,
        )

    def build_spawn_session(
        self,
        *,
        role: str,
        env: Environment,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
        scheduler: SchedulerPort | None = None,
        task: str | None = None,
        context: str = "",
    ) -> Session:
        role = validate_role_identity(role)
        cfg = self._cfg
        safety_policy = (
            cfg.safety_policy_factory(env)
            if cfg.safety_policy_factory is not None
            else None
        )
        context_builder = self._fresh_context_builder()
        plan = context_builder.build_plan(role, task=task, context=context)
        agent = context_builder.build_agent(
            role,
            scheduler=scheduler,
            # Never: a spawned agent is not the entry role, and a peer has no
            # human of its own to ask.
            ask_user_available=False,
            allow_unisolated_shell=self._unisolated_shell_allowed(
                seated_at_start=self._prebuilt_roster
            ),
            allow_unisolated_tests=self._allow_unisolated_child_tests,
            plan=plan,
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
            ask_policy=cfg.ask_policy,
            safety_policy=safety_policy,
            auto_save_path=auto_save_path,
            llm_timeout=cfg.llm_timeout,
            provider_retry_budget=self._provider_retry_budget,
            aid=aid,
            seed_user_messages=plan.startup_user_messages(),
            seed_system_messages=plan.startup_system_messages(),
            team_budget_exhausted=_team_budget_guard(scheduler),
        )

    def create_lead_session(
        self,
        *,
        scheduler: SchedulerPort,
        launch: LaunchSpec,
        budget: int,
        aid: int = 0,
    ) -> Session:
        """Build agent 0 with its local env, entry-role tools, and prompt.

        The entry role comes from the team config (``team_cfg.entry``), so agent
        0 is whichever root the team declares — not a hardcoded ``lead``. The
        scheduler-bound tools are resolved against ``scheduler`` here, so the
        lead<->scheduler cycle is closed inside this single handshake.
        ``launch.auto_save_path`` wires the auto-save subscriber; resume/seed is
        left to ``Session.apply_launch``.
        """
        cfg = self._cfg
        env = LocalEnvironment(self._lead_workspace)
        context_builder = self._fresh_context_builder()
        plan = context_builder.build_plan(self._team.entry)
        agent = context_builder.build_agent(
            self._team.entry,
            scheduler=scheduler,
            ask_user_available=self._interactive,
            allow_unisolated_shell=self._unisolated_shell_allowed(
                seated_at_start=True
            ),
            plan=plan,
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            ask_policy=cfg.ask_policy,
            safety_policy=build_workspace_safety_policy(env),
            auto_save_path=launch.auto_save_path,
            llm_timeout=cfg.llm_timeout,
            provider_retry_budget=self._provider_retry_budget,
            aid=aid,
            seed_system_messages=plan.startup_system_messages(),
            team_budget_exhausted=_team_budget_guard(scheduler),
        )


__all__ = [
    "ORCHESTRATION_FILENAME",
    "WORKFLOW_MANIFEST_FILENAME",
    "WORKFLOW_RUN_PREFIX",
    "DefaultSessionFactory",
    "SnapshotSessionError",
    "agent_save_path",
    "build_session",
    "build_spawn_session",
    "load_session",
    "make_run_dir",
    "slug_label",
    "snapshot_session",
    "validate_responses_model_controls",
    "workflow_transcript_path",
]
