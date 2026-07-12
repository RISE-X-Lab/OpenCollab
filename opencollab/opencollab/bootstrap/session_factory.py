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
import os
import re
import stat
import uuid
from datetime import datetime
from typing import Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.repo_map import build_repo_map
from opencollab.adapters.trace import Tracer
from opencollab.application.autosave import AutoSaveSubscriber
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
from opencollab.bootstrap.team_config import TeamConfig, default_team_config
from opencollab.domain.agent import Agent
from opencollab.domain.identity import role_storage_slug, validate_role_identity


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
    a glance even though they share this parent dir. A 4-char suffix is appended
    if a same-second folder already exists, so two runs started within the same
    second do not collide.
    """
    if (
        not isinstance(prefix, str)
        or "/" in prefix
        or "\\" in prefix
        or any(ord(character) < 32 or ord(character) == 127 for character in prefix)
        or len(prefix.encode("utf-8", errors="surrogatepass")) > 32
    ):
        raise ValueError("run directory prefix must be one short safe component")
    workspace_root = os.path.realpath(workspace)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        workspace_fd = os.open(workspace_root, directory_flags)
    except OSError as exc:
        raise ValueError(f"workspace is not a real directory: {workspace}") from exc
    opencollab_fd = -1
    sessions_fd = -1
    base = os.path.join(workspace_root, ".opencollab", "sessions")
    try:
        opencollab_fd = _open_or_create_child_directory(
            workspace_fd,
            ".opencollab",
        )
        sessions_fd = _open_or_create_child_directory(
            opencollab_fd,
            "sessions",
        )
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        for attempt in range(1_000):
            suffix = "" if attempt == 0 else f"-{uuid.uuid4().hex[:8]}"
            name = f"{prefix}{stamp}{suffix}"
            try:
                os.mkdir(name, 0o700, dir_fd=sessions_fd)
            except FileExistsError:
                continue
            run_fd = -1
            verify_opencollab_fd = -1
            verify_sessions_fd = -1
            verify_run_fd = -1
            try:
                run_fd = _open_existing_child_directory(sessions_fd, name)
                verify_opencollab_fd = _open_existing_child_directory(
                    workspace_fd,
                    ".opencollab",
                )
                verify_sessions_fd = _open_existing_child_directory(
                    verify_opencollab_fd,
                    "sessions",
                )
                verify_run_fd = _open_existing_child_directory(
                    verify_sessions_fd,
                    name,
                )
                pairs = (
                    (opencollab_fd, verify_opencollab_fd),
                    (sessions_fd, verify_sessions_fd),
                    (run_fd, verify_run_fd),
                )
                if any(
                    (os.fstat(original).st_dev, os.fstat(original).st_ino)
                    != (os.fstat(verified).st_dev, os.fstat(verified).st_ino)
                    for original, verified in pairs
                ):
                    raise OSError("run directory parent chain changed during reservation")
                return os.path.join(base, name)
            except BaseException:
                try:
                    os.rmdir(name, dir_fd=sessions_fd)
                except OSError:
                    pass
                raise
            finally:
                for fd in (
                    verify_run_fd,
                    verify_sessions_fd,
                    verify_opencollab_fd,
                    run_fd,
                ):
                    if fd >= 0:
                        os.close(fd)
    finally:
        if sessions_fd >= 0:
            os.close(sessions_fd)
        if opencollab_fd >= 0:
            os.close(opencollab_fd)
        os.close(workspace_fd)
    raise OSError("could not reserve a unique run directory")


def _open_or_create_child_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"run directory parent is not a real directory: {name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(name, flags, dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(fd)
        raise OSError(f"run directory parent changed while opening: {name}")
    return fd


def _open_existing_child_directory(parent_fd: int, name: str) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"run directory component is not a real directory: {name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(name, flags, dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(fd)
        raise OSError(f"run directory component changed while opening: {name}")
    return fd


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
    store: SessionStorePort | None = None,
    aid: int = -1,
    seed_user_messages: list[dict[str, Any]] | None = None,
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
        store=store,
        auto_save_callback=session._auto_save,
        auto_save_prepare_callback=session._prepare_auto_save,
        aid=aid,
        seed_user_messages=seed_user_messages,
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


def snapshot_session(session: Session) -> Session:
    """Build an independent ``Session`` sharing the source's state.

    Drops the original session's internal ``AutoSaveSubscriber`` but
    re-attaches any external subscriber on the bus so external observers
    keep seeing events.
    """
    external_sink: EventPublisherPort | None = None
    for target in session.event_bus.subscribers:
        if not isinstance(target, AutoSaveSubscriber):
            external_sink = target  # type: ignore[assignment]
            break
    new = build_session(
        agent=session.agent,
        env=session.env,
        tracer=session.tracer,
        max_budget_tokens=session.max_budget_tokens,
        max_steps=session.max_steps,
        event_sink=external_sink,
        permission_policy=session.permission_policy,
        safety_policy=session.tool_execution.safety_policy,
    )
    new.messages = copy.deepcopy(session.messages)
    new.used_tokens = session.used_tokens
    new.step_count = session.step_count
    return new


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

    ``team_cfg`` defaults to the lead-only default team, so roles resolve to the
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
        # Resolve the workspace's skills/ directory (FileSkillStore) or fall back
        # to a NullSkillStore. Shared by the builder for both catalog injection
        # and binding the use_skill dispatcher; repos without skills/ are
        # unaffected (empty store → no catalog, no tool offered).
        skill_store = build_skill_store(lead_workspace)
        # One repo map for the whole team: children work in the lead's
        # workspace (or worktree copies of it), so the lead's map orients all.
        project_context = build_repo_map(lead_workspace) if lead_workspace else None
        self._context_builder = ContextBuilder(
            self._team, cfg, skill_store=skill_store, project_context=project_context
        )
        self._lead_workspace = lead_workspace
        self._interactive = interactive
        # Run folder where every agent's transcript is persisted. When set,
        # spawned children get their own ``agent_<aid>_<role>.json`` autosave.
        self._save_dir = save_dir

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
        plan = self._context_builder.build_plan(role, task=task, context=context)
        agent = self._context_builder.build_agent(
            role, scheduler=scheduler, interactive=False, plan=plan
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
            aid=aid,
            seed_user_messages=plan.startup_user_messages(),
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
        agent = self._context_builder.build_agent(
            self._team.entry, scheduler=scheduler, interactive=self._interactive
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
            aid=aid,
            team_budget_exhausted=_team_budget_guard(scheduler),
        )


__all__ = [
    "ORCHESTRATION_FILENAME",
    "WORKFLOW_MANIFEST_FILENAME",
    "WORKFLOW_RUN_PREFIX",
    "DefaultSessionFactory",
    "agent_save_path",
    "build_session",
    "build_spawn_session",
    "load_session",
    "make_run_dir",
    "slug_label",
    "snapshot_session",
    "workflow_transcript_path",
]
