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
- ``build_default_tools``: canonical agent tool bundle.
- ``build_session_runtime``: the actual collaborator construction order
  (event bus, state, store, autosave, LLM, tool execution, compactor,
  run-loop use case).
- ``build_session`` / ``snapshot_session``: self-wiring application
  ``Session`` factories. Replaced the deleted ``bootstrap.session.Session``
  subclass that used to inherit from ``application.session.Session``.
- ``SpawnConfig`` + ``build_spawn_session`` +
  ``DefaultSessionFactory``: spawn-session wiring used by the scheduler.
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
from opencollab.application.scheduler import LaunchSpec, Scheduler
from opencollab.application.session import Session, SessionRuntime
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

# ---------------------------------------------------------------------------
# System prompts
#
# Collaboration patterns live in prompts, not framework code. The lead's prompt
# embeds the rules for *when* to spawn; each role prompt defines *how* that role
# behaves. ``get_role_prompt`` falls back to ``DEFAULT_ROLE_PROMPT`` for unknown
# roles.
# ---------------------------------------------------------------------------

LEAD_SYSTEM_PROMPT = """\
You are agent 0, the primary developer. You do the work directly and can spawn
specialist agents to parallelize when it helps.

You have direct tools — `bash`, `file_read`, `file_write`, `grep` — and two
agent-spawning tools:
- `spawn_agent`: Spawn a specialist agent to work on an independent sub-task. It
  runs in parallel in an isolated git worktree and its result is injected back to
  you when it completes. Use this for sub-tasks that can run concurrently.
- `spawn_with_review`: Spawn a coding task with mandatory review — a Coder
  implements, then a Reviewer verifies, retrying with feedback (up to 3 rounds).
  Use this for complex or risky code changes.

## How to work

1. **Trivial / small tasks** (typos, simple fixes, single-file edits, exploration):
   Just do them yourself with your direct tools. Don't spawn agents for these.

2. **Complex features** — Apply the Self-Collaboration pattern:
   a. Optionally spawn 'analyst' to break the request into a concrete plan.
   b. Spawn 'coder' agents for the independent steps (or use spawn_with_review for
      risky steps), letting independent work run in parallel.
   c. Synthesize the results and respond to the user.

3. **Debugging stuck loops**: If a task fails repeatedly, DO NOT retry the same
   approach. Either spawn 'reviewer' to analyze the error with fresh eyes, or ask
   the user for clarification.

4. **Parallel independence**: When spawning multiple agents, ensure they don't
   modify the same files. Each spawned agent works in an isolated worktree.

5. **Context discipline**: Spawned agents return summaries, not raw logs — this
   keeps your context clean for high-level reasoning.

## Available Specialist Roles

- `analyst`: Requirements analysis, architecture planning, task decomposition.
- `coder`: Code implementation, bug fixes, file modifications.
- `reviewer`: Code review, error analysis, quality verification.

You can also spawn custom roles by specifying a name — the system will create a
specialist with appropriate defaults.
"""

ANALYST_SYSTEM_PROMPT = """\
You are an Analyst agent. Your job is to break down complex user requests into
concrete, actionable implementation plans.

Output a numbered step-by-step plan. For each step, specify:
- What files need to be created or modified
- What the expected behavior should be
- Any dependencies between steps

Be specific and technical. The plan will be given to a Coder agent to implement.
Do NOT write code — only plan.
"""

CODER_SYSTEM_PROMPT = """\
You are a Coder agent. You implement code changes based on task descriptions.

Rules:
- Use the provided tools (bash, file_read, file_write, grep) to explore and modify code.
- Always read existing files before modifying them.
- Write clean, minimal code — no unnecessary abstractions.
- After making changes, verify them (run tests, check syntax, etc.).
- If you're stuck after 3 attempts, STOP and explain what's blocking you.
"""

REVIEWER_SYSTEM_PROMPT = """\
You are a Reviewer agent. You review code implementations for correctness.

Your review process:
1. Read the relevant files to understand the current state.
2. Check for: logic errors, edge cases, security issues, missing error handling.
3. If the implementation is correct, output exactly: PASS
4. If there are issues, output detailed fix instructions.

Be direct and specific. Don't suggest style changes — focus on correctness.
"""

ROLE_PROMPTS: dict[str, str] = {
    "analyst": ANALYST_SYSTEM_PROMPT,
    "coder": CODER_SYSTEM_PROMPT,
    "reviewer": REVIEWER_SYSTEM_PROMPT,
}

DEFAULT_ROLE_PROMPT = """\
You are a specialist agent. Complete the assigned task using the provided tools.
Be thorough but efficient. When done, provide a clear summary of what you did.
"""


def get_role_prompt(role: str) -> str:
    """Get the system prompt for a role, or default if unknown."""
    return ROLE_PROMPTS.get(role.lower(), DEFAULT_ROLE_PROMPT)


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
# Spawn session wiring
# ---------------------------------------------------------------------------


@dataclass
class SpawnConfig:
    """Shared LLM/runtime config inherited by every spawned agent session."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPort | None
    safety_policy_factory: SafetyPolicyFactory | None = None


def build_spawn_session(
    *,
    role: str,
    env: Environment,
    cfg: SpawnConfig,
    budget: int,
    max_steps: int = 50,
    aid: int = -1,
) -> Session:
    """Build the Agent + Session bundle for a spawned child agent.

    Tools are stateless; the safety policy is derived from the child's
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
    """Default ``SessionFactoryPort`` implementation used by the scheduler.

    Holds the shared ``SpawnConfig`` (LLM config inherited by every session)
    plus the lead-only composition bits (``lead_workspace`` for the local
    environment and ``interactive`` for the ask-user tool). The scheduler asks
    for sessions by role/env/budget or via ``create_lead_session`` without
    knowing how any of them are wired.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        lead_workspace: str | None = None,
        interactive: bool = False,
    ):
        self._cfg = cfg
        self._lead_workspace = lead_workspace
        self._interactive = interactive

    def build_spawn_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
    ) -> Session:
        return build_spawn_session(
            role=role,
            env=env,
            cfg=self._cfg,
            budget=budget,
            max_steps=max_steps,
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
        """Build agent 0 with its local env, full tool bundle, and prompt.

        The spawn tools are bound to ``scheduler`` here, so the lead<->scheduler
        cycle is resolved inside this single handshake (no post-construction
        tool splicing in the caller). ``launch.auto_save_path`` wires the
        auto-save subscriber; resume/seed is left to ``Session.apply_launch``.
        """
        cfg = self._cfg
        env = LocalEnvironment(self._lead_workspace)
        tools = list(build_default_tools(include_ask_user=False))
        tools[0:0] = [SpawnAgentTool(scheduler), SpawnWithReviewTool(scheduler)]
        if self._interactive:
            # Interactive mode only: ask_user stays off in headless eval
            # (ref: SWE-bench regression root cause).
            tools.append(AskUserTool())
        agent = Agent(
            name="lead",
            system_prompt=LEAD_SYSTEM_PROMPT,
            tools=tools,
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
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=build_workspace_safety_policy(env),
            auto_save_path=launch.auto_save_path,
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
) -> Scheduler:
    """Build the Scheduler and let it create agent 0 (the init process).

    The container wires dependencies and hands the scheduler a ``LaunchSpec``;
    ``scheduler.create_init_process`` builds agent 0 (aid=0) through the factory
    and applies the launch spec. ``session_file`` resumes agent 0's history;
    ``auto_save`` writes a JSONL transcript under
    ``<workspace>/.opencollab/sessions``.
    """
    cfg = ctx.config
    event_bus = EventBus(ctx.event_sink)
    session_factory = DefaultSessionFactory(
        SpawnConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            tracer=ctx.tracer,
            event_bus=event_bus,
            permission_policy=ctx.permission_policy,
            safety_policy_factory=build_workspace_safety_policy,
        ),
        lead_workspace=ctx.workspace,
        interactive=interactive,
    )
    worktree_pool = WorktreePool(ctx.workspace, use_worktrees=use_worktrees)

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=worktree_pool,
        event_sink=event_bus,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        permission_policy=ctx.permission_policy,
    )

    auto_save_path: str | None = None
    if auto_save:
        session_id = uuid.uuid4().hex[:8]
        auto_save_dir = os.path.join(ctx.workspace, ".opencollab", "sessions")
        auto_save_path = os.path.join(auto_save_dir, f"{session_id}.jsonl")

    scheduler.create_init_process(
        LaunchSpec(session_file=session_file, auto_save_path=auto_save_path)
    )
    return scheduler


__all__ = [
    "DefaultSessionFactory",
    "RuntimeContext",
    "SessionRuntime",
    "SpawnConfig",
    "build_default_tools",
    "build_runtime_context",
    "build_session",
    "build_session_runtime",
    "build_scheduler",
    "build_spawn_session",
    "build_workspace_safety_policy",
    "load_session",
    "snapshot_session",
]
