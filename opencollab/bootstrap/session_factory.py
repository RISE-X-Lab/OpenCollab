"""Build, restore and snapshot sessions for scheduler entry and child agents.

``ContextBuilder`` assembles role definitions. Shared runtime and LLM client
construction live in ``container.build_session_runtime``.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import os
import re
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.llm.providers import RESPONSES
from opencollab.adapters.llm.retry import RetryTimeBudget
from opencollab.adapters.llm.types import model_capabilities
from opencollab.adapters.repo_map import build_repo_map
from opencollab.adapters.safe_files import ensure_directory_no_symlinks
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.single2_safety import Single2SafetyPolicy
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.trace import Tracer
from opencollab.application.compaction_summary import run_coro_blocking
from opencollab.application.event_bus import EventBus
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import (
    AskUserPort,
    AutosaveOwnershipStorePort,
    EventPublisherPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SchedulerPort,
    SessionStorePort,
    ShaperPort,
)
from opencollab.application.run_control import RunControl
from opencollab.application.scheduler import LaunchSpec
from opencollab.application.session import Session, _acquire_autosave_writer, _read_session_snapshot
from opencollab.bootstrap.agent_profiles import (
    SingleAgentProfile,
    resolve_agent_profile,
)
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
from opencollab.domain.session import SessionPhase


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

# Workflow folders hold ``<seq>_<role>.json`` transcripts,
# ``orchestration.jsonl`` signals, and a ``workflow.json`` manifest.
ORCHESTRATION_FILENAME = "orchestration.jsonl"
WORKFLOW_MANIFEST_FILENAME = "workflow.json"

# The ``wf-`` prefix distinguishes workflow folders from team folders.
WORKFLOW_RUN_PREFIX = "wf-"

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
    max_budget_tokens: int | None = 1_000_000,
    max_steps: int | None = 100,
    auto_save_path: str | None = None,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    ask_policy: AskUserPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    llm_timeout: float | None = 600.0,
    provider_retry_budget: RetryTimeBudget | None = None,
    store: SessionStorePort | None = None,
    aid: int = -1,
    seed_user_messages: list[dict[str, Any]] | None = None,
    seed_system_messages: list[dict[str, Any]] | None = None,
    shaper: ShaperPort | None = None,
    team_budget_exhausted: Callable[[], bool] | None = None,
    agent_profile: Any | None = None,
    context_policy: Any | None = None,
    run_id: str | None = None,
    run_control: RunControl | None = None,
    default_cancel_event: asyncio.Event | None = None,
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
        agent_profile=agent_profile,
        context_policy=context_policy,
        run_id=run_id,
        run_control=run_control,
    )
    Session.__init__(
        session,
        agent=agent,
        runtime=runtime,
        env=env,
        tracer=tracer,
        max_budget_tokens=runtime.runner.max_budget_tokens,
        max_steps=max_steps,
        auto_save_path=auto_save_path,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
        default_cancel_event=default_cancel_event,
    )
    return session

def load_session(
    path: str,
    agent: Agent,
    **kwargs: Any,
) -> Session:
    """Build a session and restore the snapshot at ``path``.

    Omitted ``aid`` and ``run_id`` values come from the snapshot, so records
    written during construction and later turns retain the saved identity.
    """
    store = kwargs.get("store")
    if store is None:
        store = SessionStore()
    target = kwargs.get("auto_save_path")
    session = None
    try:
        with ExitStack() as ownership:
            if target and isinstance(store, AutosaveOwnershipStorePort):
                ownership.callback(_acquire_autosave_writer(store, target))
            snapshot = _read_session_snapshot(store, path, agent.system_prompt)
            options = {**_snapshot_identity(snapshot), **kwargs, "store": store}
            session = build_session(agent=agent, **options)
            session._restore_snapshot(path, snapshot, aid=kwargs.get("aid"))
            return session
    except BaseException as error:
        if session is not None:
            restore_error = error

            async def rollback() -> None:
                operations = [("session", session.aclose)]
                if kwargs.get("env") is None and session.env is not None:
                    operations.append(("environment", session.env.cleanup))
                for label, close in operations:
                    try:
                        await close()
                    except BaseException as failure:
                        add_exception_note(
                            restore_error, f"restore {label} cleanup also failed: {type(failure).__name__}: {failure}",
                        )

            try:
                run_coro_blocking(rollback)
            except BaseException as failure:
                add_exception_note(error, f"restore rollback also failed: {type(failure).__name__}: {failure}")
        raise

def _snapshot_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    run_id = snapshot.get("run_id")
    if isinstance(run_id, str) and run_id.strip() and "\x00" not in run_id:
        identity["run_id"] = run_id
    try:
        identity["aid"] = int(snapshot["aid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return identity

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
    from opencollab.bootstrap.authored_workflow import AuthoredWorkflowTool
    from opencollab.bootstrap.team_workflow import TeamWorkflowTool

    if session._llm_closed:
        raise SnapshotSessionError("cannot snapshot a session with closed model ownership")
    session_tools = (*session.agent.tools, *session.runner._initial_agent_tools)
    # Exclude callable owners from deepcopy. Python 3.14 functions bypass memo lookup.
    owner_bindings = {
        id(tool._get_session): lambda: new
        for tool in session_tools
        if isinstance(tool, (AuthoredWorkflowTool, TeamWorkflowTool))
    }
    for tool in session_tools:
        if isinstance(tool, AuthoredWorkflowTool):
            # Keep configured provider owners outside the session's independently copied state.
            for callback in (tool._environment_pool_factory, tool._environment_catalog, tool._describe_environment):
                if callback is not None:
                    owner_bindings[id(callback)] = callback
    for tool in session_tools:
        if isinstance(tool, (AuthoredWorkflowTool, TeamWorkflowTool)) and id(tool._config) not in owner_bindings:
            config = replace(tool._config, tracer=None, event_bus=EventBus())
            owner_bindings[id(tool._config)] = _clone_snapshot_component(
                config, label="workflow configuration", memo=owner_bindings,
            )
    agent, initial_tools, initial_tool_choice = _clone_snapshot_component(
        (session.agent, session.runner._initial_agent_tools, session.runner._initial_agent_tool_choice),
        label="agent",
        memo=owner_bindings,
    )
    for tool in (*agent.tools, *initial_tools):
        if isinstance(tool, (AuthoredWorkflowTool, TeamWorkflowTool)):
            tool._get_session = lambda: new
        if isinstance(tool, AuthoredWorkflowTool):
            tool._registered = False
    permission_policy = _clone_snapshot_component(session.permission_policy, label="permission policy")
    ask_policy = _clone_snapshot_component(session.tool_execution.ask_policy, label="ask policy")
    safety_policy = _clone_snapshot_component(session.tool_execution.safety_policy, label="safety policy")
    state = _clone_snapshot_component(session.state, label="session state")
    environment = _fork_snapshot_environment(session.env)
    try:
        _rebind_snapshot_safety_policy(safety_policy, session.env.workspace, environment.workspace)
        control = session.runner._run_control
        if control is not None:
            control = replace(control, initial_soft_budget_tokens=session.max_budget_tokens, on_event=None)
        new = build_session(
            agent=agent,
            env=environment,
            max_budget_tokens=session.runner.hard_budget_tokens,
            max_steps=session.max_steps,
            event_sink=event_sink,
            permission_policy=permission_policy,
            ask_policy=ask_policy,
            safety_policy=safety_policy,
            llm=session._llm,
            llm_timeout=session.runner._per_call_timeout,
            aid=session.state.aid,
            run_id=session.run_id,
            run_control=control,
        )
        new.max_budget_tokens = session.max_budget_tokens
        # Soft-budget changes preserve the source's already-effective reserve.
        new.runner._enforcement_strength = session.runner._enforcement_strength
        new.runner._commit_reserve = session.runner._commit_reserve
        if state.phase not in {
            SessionPhase.IDLE, SessionPhase.DONE, SessionPhase.STOPPED, SessionPhase.ERROR,
        } or new._open_tool_call_ids(state):
            new._normalize_forked_state(state, session._fork_runtime_metadata())
            new.runner.reset_for_restore()
        # Keep the current turn's restrictions through normalization, then retain
        # the original capabilities for the next user turn.
        new.runner._initial_agent_tools = initial_tools
        new.runner._initial_agent_tool_choice = initial_tool_choice
        new.state = state
        new.runner.state = state
        new.tool_execution.state = state
        # Clone the effective pipeline, including injected shapers and live caches.
        # Session-owned collaborators resolve to this branch's existing owners.
        memo = {
            id(session): new,
            id(session.state): state,
            id(session.runner): new.runner,
            id(session.tool_execution): new.tool_execution,
            id(session.event_bus): new.event_bus,
            id(session.agent): new.agent,
            id(session.env): new.env,
            id(session._llm): session._llm,
        }
        if session._context_summarizer is not None:
            memo[id(session._context_summarizer)] = new._context_summarizer
        shaper = _clone_snapshot_component(session.runner.shaper, label="context shaper", memo=memo)
        if shaper is not None and shaper is session.runner.shaper:
            raise SnapshotSessionError("cannot clone context shaper for an independent snapshot")
        new.runner.shaper = shaper
        session._share_llm_ownership(new)
        return new
    except BaseException as error:
        try:
            # Finish rollback before the synchronous caller loses the new owner.
            run_coro_blocking(environment.cleanup)
        except BaseException as cleanup_error:
            add_exception_note(
                error,
                f"snapshot environment cleanup failed for {environment.workspace!r} "
                f"with {type(cleanup_error).__name__} ({cleanup_error})",
            )
        raise

def _rebind_snapshot_safety_policy(policy: Any, source_workspace: str, workspace: str) -> None:
    def relocate(path: str) -> str:
        source_root = os.path.realpath(source_workspace)
        absolute = os.path.realpath(path)
        if os.path.commonpath((source_root, absolute)) == source_root:
            return os.path.abspath(os.path.join(workspace, os.path.relpath(absolute, source_root)))
        return path

    if isinstance(policy, Single2SafetyPolicy):
        policy.root = relocate(policy.root)
        _rebind_snapshot_safety_policy(policy._policy, source_workspace, workspace)
    elif isinstance(policy, SandboxInterceptor):
        policy.root = relocate(policy.root)
        policy.allowed_roots = [relocate(root) for root in policy.allowed_roots]

def _clone_snapshot_component(value: Any, *, label: str, memo: dict[int, Any] | None = None) -> Any:
    try:
        return copy.deepcopy(value, memo)
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

SESSION_MAX_STEPS = 100
"""The step ceiling every session in a run is built with.

A runaway guard, not an allowance. Steps and tokens cannot both be equalized
across differently-organized runs: a solo agent carries one long history and
pays more per step, while a team's members each carry a short one and pay less,
so holding the token pool equal makes the team's step count higher and holding
the step count equal makes its token spend lower. Exactly one of the two can be
the aligned resource, and it is tokens — that is the cost, the thing the
provider bills, and the thing a budget claim is about. Steps are counted and
reported instead, which makes them a result rather than a control.

A guard is therefore only useful if it never fires: a session stopped by this
ceiling while it still held tokens would be a limit acting under the name of a
statistic. It has to sit above what the pool can physically fund, and every
step must send the system prompt and the tool schemas, which compaction cannot
remove — so the cheapest possible step has a floor and the pool divided by that
floor bounds the steps a run can reach. Measured on the arms this repository
ships, that bound is under 500 steps for a 1,000,000-token pool.

The value is the same for every seat. A teammate used to get half the entry
agent's allowance for no reason anyone could state, which is the same kind of
unearned privilege difference the shell gate had.
"""

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
    """Build a child session through ``DefaultSessionFactory``.

    ``team_cfg`` defaults to the Self-Collaboration team. Safety follows the
    child environment. ``task``, when supplied, seeds its first user-context message.
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
    """Build scheduler sessions from shared configuration and role definitions.

    ``ContextBuilder`` assembles agents from SpawnConfig and TeamConfig.
    ``lead_environment`` supplies agent 0's environment, otherwise it uses the
    host ``lead_workspace``. ``interactive`` controls the entry ask-user tool.

    ``allow_unisolated_shell`` authorizes unsandboxed commands for initial seats, with
    None inheriting interactive. ``prebuilt_roster`` distinguishes those seats
    from dynamic children. ``allow_unisolated_child_shell`` separately authorizes
    child unsandboxed commands, defaults to False and preserves command confirmation.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        team_cfg: TeamConfig | None = None,
        environment_pool_factory: Callable[..., Any] | None = None,
        environment_catalog: Callable[..., dict[str, Any]] | None = None,
        lead_workspace: str | None = None,
        lead_environment: Environment | None = None,
        interactive: bool = False,
        save_dir: str | None = None,
        prebuilt_roster: bool = False,
        allow_unisolated_shell: bool | None = None,
        allow_unisolated_child_shell: bool = False,
        max_steps: int | None = SESSION_MAX_STEPS,
        run_id: str | None = None,
    ):
        self._cfg = cfg
        self._provider_retry_budget = (
            RetryTimeBudget(cfg.provider_error_time_budget)
            if cfg.provider_error_time_budget > 0
            else None
        )
        self._team = team_cfg or default_team_config()
        self._environment_pool_factory = environment_pool_factory
        self._environment_catalog = environment_catalog
        self._workflows = None
        if any("run_workflow" in role.tools for role in self._team.roles.values()):
            from opencollab.bootstrap.workflow_runtime import discover_workflows

            directory = os.environ.get("OPENCOLLAB_WORKFLOWS_DIR", "workflows")
            if not os.path.isabs(directory):
                directory = os.path.join(lead_workspace or os.getcwd(), directory)
            self._workflows = discover_workflows(directory, include_builtin=True)
        self._interactive = interactive
        self._validate_responses_tool_support()
        self._lead_workspace = lead_workspace
        self._lead_environment = lead_environment
        self._owns_lead_environment = lead_environment is None
        self._prebuilt_roster = bool(prebuilt_roster)
        self._allow_unisolated_child_shell = bool(allow_unisolated_child_shell)
        self._allow_unisolated_shell = (
            interactive if allow_unisolated_shell is None else bool(allow_unisolated_shell)
        )
        # Run folder where every agent's transcript is persisted. When set,
        # spawned children get their own ``agent_<aid>_<role>.json`` autosave.
        self._save_dir = save_dir
        self._max_steps = None if max_steps is None else int(max_steps)
        self._run_id = run_id

    def _validate_responses_tool_support(self) -> None:
        """Reject statically incompatible team roles before opening a workspace."""
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
            settings = self._cfg.model_settings(role.model)
            if settings["wire_protocol"] != RESPONSES:
                continue
            model = settings["model"]
            tool_names = set(role.tools)
            if role_name != self._team.entry or not self._interactive:
                tool_names.discard("ask_user")
            validate_responses_model_controls(
                model,
                role_name=role_name,
                tools_present=bool(tool_names),
                top_p=self._cfg.top_p,
                reasoning_effort=self._cfg.reasoning_effort_for_role(role),
            )
        fallback_tool_names = set(BASE_TOOL_NAMES) - {"ask_user"}
        default_settings = self._cfg.model_settings()
        if self._team.topology.allow_all and default_settings["wire_protocol"] == RESPONSES:
            capabilities = model_capabilities(default_settings["model"])
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
        """Resolve unsandboxed-command permission independently of human interaction.

        Agent 0 and prebuilt teammates use ``allow_unisolated_shell``, with None
        inheriting interactive. Dynamic children require a sandboxed environment
        unless ``allow_unisolated_child_shell`` explicitly permits unsandboxed commands.
        ``ask_user`` follows the separate interactive entry-role rule.
        """
        if not seated_at_start:
            return self._allow_unisolated_child_shell
        return self._allow_unisolated_shell

    def _lead_workspace_is_readable_by_the_agents(self) -> bool:
        """Whether the agents' repository can be mapped on this host.

        A local environment's workspace holds the repository. The lead workspace
        anchors configuration and may point elsewhere.
        """
        env = self._lead_environment
        if env is None:
            return True
        return bool(getattr(env, "local_filesystem", False))

    def _fresh_context_builder(self) -> ContextBuilder:
        """Snapshot bounded workspace context at the new session's start."""
        skill_store = build_skill_store(self._lead_workspace)
        project_workspace = (
            self._lead_environment.workspace
            if self._lead_environment is not None
            else self._lead_workspace
        )
        project_context = (
            build_repo_map(project_workspace)
            if project_workspace and self._lead_workspace_is_readable_by_the_agents()
            else None
        )
        return ContextBuilder(
            self._team,
            self._cfg,
            skill_store=skill_store,
            project_context=project_context,
            prebuilt_team=self._prebuilt_roster,
        )

    def build_spawn_session(
        self,
        *,
        role: str,
        env: Environment,
        budget: int,
        max_steps: int | None = None,
        aid: int = -1,
        scheduler: SchedulerPort | None = None,
        task: str | None = None,
        context: str = "",
    ) -> Session:
        role = validate_role_identity(role)
        # ``None`` inherits the run ceiling. Explicit values retain the standalone
        # ``build_spawn_session`` override.
        max_steps = self._max_steps if max_steps is None else int(max_steps)
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
            workflow_tool=self._workflow_tool(lambda: session),
            authored_workflow_tool=self._authored_workflow_tool(
                lambda: session, seated_at_start=self._prebuilt_roster,
            ),
            # Spawned agents have no independent human to answer their questions.
            ask_user_available=False,
            allow_unisolated_shell=self._unisolated_shell_allowed(
                seated_at_start=self._prebuilt_roster
            ),
            plan=plan,
        )
        auto_save_path = (
            agent_save_path(self._save_dir, aid, role)
            if self._save_dir
            else None
        )
        session = build_session(
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
            context_policy=self._team.context,
            agent_profile=self._role_profile(role),
            run_id=self._run_id,
        )
        return session

    def _workflow_tool(self, get_session: Callable[[], Session]) -> Tool | None:
        if self._workflows is None:
            return None
        from opencollab.bootstrap.team_workflow import TeamWorkflowTool

        return TeamWorkflowTool(get_session, self._workflows, self._cfg, self._save_dir)

    def _authored_workflow_tool(
        self, get_session: Callable[[], Session], *, seated_at_start: bool,
    ) -> Tool:
        from opencollab.bootstrap.authored_workflow import AuthoredWorkflowTool

        return AuthoredWorkflowTool(
            get_session, self._cfg, self._save_dir,
            environment_pool_factory=self._environment_pool_factory,
            describe_environment=self._environment_catalog,
            allow_unisolated_shell=self._unisolated_shell_allowed(seated_at_start=seated_at_start),
        )

    def _role_profile(self, role_name: str) -> SingleAgentProfile | None:
        """The agent profile this seat runs under, or ``None`` for OpenCollab's.

        The prompt half of a profile is folded in by ``ContextBuilder``; this is
        the other half — the shaper and the safety wrapper, which the session
        runtime applies. Both halves read the same declaration, so a seat cannot
        take a profile's words while running OpenCollab's own history handling.
        """
        profile = self._team.role_for(role_name).profile
        return resolve_agent_profile(profile) if profile is not None else None

    @property
    def lead_environment(self) -> Environment:
        """Share the lead workspace with bootstrap's environment owner and probes."""
        if self._lead_environment is None:
            self._lead_environment = LocalEnvironment(self._lead_workspace)
        return self._lead_environment

    def _discard_unstarted_lead_environment(self) -> None:
        if self._owns_lead_environment and self._lead_environment is not None:
            self._lead_environment._close_workspace()
            self._lead_environment = None

    def create_lead_session(
        self,
        *,
        scheduler: SchedulerPort,
        launch: LaunchSpec,
        budget: int,
        aid: int = 0,
    ) -> Session:
        """Build agent 0 from the configured entry role and scheduler-bound tools.

        ``launch.auto_save_path`` wires autosave. ``Session.apply_launch`` handles
        resume or seed messages.
        """
        cfg = self._cfg
        # Agent 0 shares the supplied environment or the lead host workspace.
        env = self.lead_environment
        try:
            context_builder = self._fresh_context_builder()
            plan = context_builder.build_plan(self._team.entry)
            agent = context_builder.build_agent(
                self._team.entry,
                scheduler=scheduler,
                workflow_tool=self._workflow_tool(lambda: session),
                authored_workflow_tool=self._authored_workflow_tool(lambda: session, seated_at_start=True),
                ask_user_available=self._interactive,
                allow_unisolated_shell=self._unisolated_shell_allowed(
                    seated_at_start=True
                ),
                plan=plan,
            )
            session = build_session(
                agent=agent,
                env=env,
                tracer=cfg.tracer,
                max_budget_tokens=budget,
                max_steps=self._max_steps,
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
                context_policy=self._team.context,
                agent_profile=self._role_profile(self._team.entry),
                run_id=self._run_id,
            )
            return session
        except BaseException:
            self._discard_unstarted_lead_environment()
            raise


__all__ = [
    "ORCHESTRATION_FILENAME",
    "WORKFLOW_MANIFEST_FILENAME",
    "WORKFLOW_RUN_PREFIX",
    "DefaultSessionFactory",
    "SnapshotSessionError",
    "agent_save_path",
    "build_session",
    "SESSION_MAX_STEPS",
    "build_spawn_session",
    "load_session",
    "make_run_dir",
    "slug_label",
    "snapshot_session",
    "validate_responses_model_controls",
    "workflow_transcript_path",
]
