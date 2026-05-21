"""Composition root for OpenCollab.

Single file that knows how to wire every concrete adapter into the
application use cases. CLI entry points (chat/team) and the harness build
their session/team objects through the factory functions exposed here.

Contents (top to bottom):
- ``RuntimeContext`` + ``build_runtime_context``: per-invocation context
  (workspace, config, tracer, UI hooks).
- ``build_workspace_safety_policy``: derives a sandbox interceptor from
  an Environment instance.
- ``build_default_tools``: canonical agent tool bundle.
- ``build_session_runtime``: the actual collaborator construction order
  (event bus, state, store, autosave, LLM, tool execution, compactor,
  run-loop use case).
- ``build_session`` / ``snapshot_session``: self-wiring application
  ``Session`` factories. Replaced the deleted ``bootstrap.session.Session``
  subclass that used to inherit from ``application.session.Session``.
- ``TeammateConfig`` + ``build_teammate_session`` +
  ``DefaultSessionFactory``: teammate-session wiring for spawned agents.
- ``build_scheduler``: the high-level CLI entry point — builds agent 0 (the
  lead) plus the Scheduler that runs and spawns child agents.
"""

from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.llm import LLMClient, estimate_messages_tokens
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.human import AskUserTool
from opencollab.adapters.tools.spawn import SpawnAgentTool, SpawnWithReviewTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.compaction import (
    DEFAULT_COMPACTION_THRESHOLD,
    ContextCompactionUseCase,
)
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import (
    EventPublisherPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyFactory,
    SafetyPolicyPort,
    SessionStorePort,
    TracePort,
)
from opencollab.application.scheduler import Scheduler
from opencollab.application.session import Session, SessionRuntime
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.team_prompts import LEAD_SYSTEM_PROMPT, get_role_prompt
from opencollab.application.tool_execution import ToolExecutionUseCase
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
    store: SessionStorePort | None = None,
    aid: int = -1,
) -> Session:
    """Self-wiring ``Session`` factory.

    Replaces the old ``bootstrap.session.Session`` subclass that inherited
    from ``application.session.Session`` purely to auto-build its runtime.
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
# Teammate session wiring
# ---------------------------------------------------------------------------


@dataclass
class TeammateConfig:
    """Shared LLM/runtime config every teammate inherits from the Team."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPort | None
    safety_policy_factory: SafetyPolicyFactory | None = None


def build_teammate_session(
    *,
    role: str,
    env: Environment,
    cfg: TeammateConfig,
    budget: int,
    max_steps: int = 50,
    aid: int = -1,
) -> Session:
    """Build the teammate Agent + Session bundle.

    Tools are stateless; safety policy wiring is derived from the teammate
    environment and passed into the Session.
    """
    safety_policy = (
        cfg.safety_policy_factory(env)
        if cfg.safety_policy_factory is not None
        else None
    )

    agent = Agent(
        name=role,
        system_prompt=get_role_prompt(role),
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg.model,
        provider=cfg.provider,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
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
        aid=aid,
    )


class DefaultSessionFactory:
    """Default ``SessionFactoryPort`` implementation used by ``Team``.

    Holds the shared ``TeammateConfig`` so the team orchestrator can build
    teammate sessions by role/env/budget without re-supplying LLM config.
    Also exposes ``build_lead_session`` so the orchestrator can construct
    the Lead session without importing ``Session`` directly.
    """

    def __init__(self, cfg: TeammateConfig):
        self._cfg = cfg

    def build_teammate_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
    ) -> Session:
        return build_teammate_session(
            role=role,
            env=env,
            cfg=self._cfg,
            budget=budget,
            max_steps=max_steps,
            aid=aid,
        )

    def build_lead_session(
        self,
        *,
        agent: Agent,
        env: Environment,
        tracer: Tracer | None,
        max_budget_tokens: int,
        event_sink: Any,
        permission_policy: PermissionPort | None,
        safety_policy: Any,
        max_steps: int | None = None,
        auto_save_path: str | None = None,
    ) -> Session:
        kwargs: dict[str, Any] = dict(
            agent=agent,
            env=env,
            tracer=tracer,
            max_budget_tokens=max_budget_tokens,
            event_sink=event_sink,
            permission_policy=permission_policy,
            safety_policy=safety_policy,
            auto_save_path=auto_save_path,
        )
        if max_steps is not None:
            kwargs["max_steps"] = max_steps
        return build_session(**kwargs)


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
) -> Scheduler:
    """Build the Scheduler with agent 0 (the lead) registered.

    Agent 0 is the single interactive entry: it has the direct tools plus
    ``spawn_agent`` / ``spawn_with_review``, and can spawn child agents.

    interactive=True appends AskUserTool to lead_agent.tools post-construction
    (keeps headless eval clean — ref: SWE-bench regression root cause).

    ``session_file`` resumes agent 0's message history; ``auto_save`` writes a
    JSONL transcript under ``<workspace>/.opencollab/sessions``.
    """
    cfg = ctx.config
    event_bus = EventBus(ctx.event_sink)
    session_factory = DefaultSessionFactory(
        TeammateConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            tracer=ctx.tracer,
            event_bus=event_bus,
            permission_policy=ctx.permission_policy,
            safety_policy_factory=build_workspace_safety_policy,
        )
    )
    lead_env = LocalEnvironment(ctx.workspace)
    lead_tools = build_default_tools(include_ask_user=False)
    worktree_pool = WorktreePool(ctx.workspace, use_worktrees=use_worktrees)

    lead_agent = Agent(
        name="lead",
        system_prompt=LEAD_SYSTEM_PROMPT,
        tools=list(lead_tools),
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )

    auto_save_path: str | None = None
    if auto_save:
        session_id = uuid.uuid4().hex[:8]
        auto_save_dir = os.path.join(ctx.workspace, ".opencollab", "sessions")
        auto_save_path = os.path.join(auto_save_dir, f"{session_id}.jsonl")

    lead_session = session_factory.build_lead_session(
        agent=lead_agent,
        env=lead_env,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        event_sink=event_bus,
        permission_policy=ctx.permission_policy,
        safety_policy=build_workspace_safety_policy(lead_env),
        auto_save_path=auto_save_path,
    )

    if session_file and os.path.exists(session_file):
        lead_session.messages = lead_session.store.load_messages(
            session_file, lead_agent.system_prompt
        )
    elif auto_save_path:
        lead_session.save(auto_save_path)

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=worktree_pool,
        event_sink=event_bus,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        permission_policy=ctx.permission_policy,
    )
    scheduler.register_lead(lead_session)

    lead_agent.tools[0:0] = [
        SpawnAgentTool(scheduler),
        SpawnWithReviewTool(scheduler),
    ]
    if interactive:
        # Interactive mode: give Lead the ask_user tool (not added in Scheduler
        # to keep headless eval clean — ref: SWE-bench regression root cause)
        lead_agent.tools.append(AskUserTool())
    return scheduler


__all__ = [
    "DefaultSessionFactory",
    "RuntimeContext",
    "SessionRuntime",
    "TeammateConfig",
    "build_default_tools",
    "build_runtime_context",
    "build_session",
    "build_session_runtime",
    "build_scheduler",
    "build_teammate_session",
    "build_workspace_safety_policy",
    "load_session",
    "snapshot_session",
]
