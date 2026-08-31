"""Composition root for OpenCollab.

Wires concrete adapters into the application use cases. This module owns the
session-construction *core* — the only place ``LLMClient`` is instantiated, so
``build_session_runtime`` and its model-client wiring live here together (tests
monkeypatch ``container.LLMClient`` and rely on that). The rest of the
composition root is split into focused siblings and re-exported here so
``from opencollab.bootstrap.container import X`` keeps resolving every name:

- ``tool_registry``      — tool-name -> Tool resolution + curated name sets.
- ``context_builder``    — ``SpawnConfig`` + ``ContextBuilder`` (role -> Agent).
- ``runtime_context``    — ``RuntimeContext`` + workspace safety policy.
- ``session_factory``    — ``build_spawn_session`` / ``DefaultSessionFactory`` +
                           run-folder transcript helpers.
- ``scheduler_factory``  — ``build_scheduler`` (the CLI and SDK entry point).

``session_factory`` and ``scheduler_factory`` import back from this module, so
they are re-exported lazily via module ``__getattr__`` (PEP 562) to keep import
order acyclic regardless of which module is imported first.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.llm import LLMClient, is_context_overflow_error
from opencollab.adapters.skills.file_skill_store import FileSkillStore
from opencollab.adapters.skills.null_skill_store import NullSkillStore
from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.compaction_summary import ReadTimeSummarizer
from opencollab.application.event_bus import EventBus
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import (
    AskUserPort,
    EventPublisherPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SessionStorePort,
    ShaperPort,
    SkillStorePort,
    TracePort,
)
from opencollab.application.session import SessionRuntime
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.shaping import (
    DEFAULT_TOOL_RESULT_BUDGET,
    AutoCompactShaper,
    OldHistorySnipShaper,
    PerToolResultBudgetShaper,
    ShaperPipeline,
    ToolOutputClearShaper,
    approx_messages_tokens,
    history_trigger_target,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.runtime_context import (
    RuntimeContext,
    build_runtime_context,
    build_workspace_safety_policy,
)
from opencollab.bootstrap.tool_registry import (
    COMPACTABLE_TOOL_NAMES,
    build_tools_for_role,
)
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

if TYPE_CHECKING:
    # Re-exported at runtime via ``__getattr__`` (see bottom of module); declared
    # here so static tooling sees them as defined names in ``__all__``.
    from opencollab.bootstrap.scheduler_factory import build_scheduler
    from opencollab.bootstrap.session_factory import (
        DefaultSessionFactory,
        SnapshotSessionError,
        agent_save_path,
        build_session,
        build_spawn_session,
        load_session,
        make_run_dir,
        snapshot_session,
    )

# ---------------------------------------------------------------------------
# Session runtime construction (the LLMClient-bound core)
# ---------------------------------------------------------------------------


def _build_initial_state(
    agent: Agent,
    seed_user_messages: list[dict[str, Any]] | None = None,
    seed_system_messages: list[dict[str, Any]] | None = None,
) -> SessionState:
    messages: list[dict[str, Any]] = (
        list(seed_system_messages)
        if seed_system_messages
        else [{"role": "system", "content": agent.system_prompt}]
    )
    if seed_user_messages:
        messages.extend(seed_user_messages)
    return SessionState(messages=messages)


def _resolve_llm(
    agent: Agent,
    llm: LLMPort | None,
    llm_timeout: float,
    provider_retry_budget: Any | None = None,
) -> LLMPort:
    """The injected ``llm`` if given, else a fresh ``LLMClient`` for the agent."""
    if llm is not None:
        return llm
    return LLMClient(
        model=agent.model,
        api_key=agent.api_key,
        base_url=agent.base_url,
        provider=agent.provider,
        wire_protocol=getattr(agent, "wire_protocol", "chat_completions"),
        max_retries=getattr(agent, "llm_max_retries", 3),
        request_timeout=llm_timeout,
        connect_timeout=getattr(agent, "llm_connect_timeout", 30.0),
        first_event_timeout=getattr(agent, "llm_first_event_timeout", 180.0),
        stream_idle_timeout=getattr(agent, "llm_stream_idle_timeout", 180.0),
        stream_chat=getattr(agent, "llm_stream_chat", False),
        context_window=getattr(agent, "context_window", None),
        provider_error_time_budget=getattr(agent, "provider_error_time_budget", 0.0),
        provider_retry_budget=provider_retry_budget,
    )


def _build_summarizer(
    agent: Agent,
    llm: LLMPort | None,
    resolved_llm: LLMPort,
    llm_timeout: float,
    auto_save_path: str | None,
    provider_retry_budget: Any | None = None,
) -> ReadTimeSummarizer:
    """Build the read-time summarizer that powers ``AutoCompactShaper``.

    Read-time path owns compaction (Option B): wire the structured handoff
    prompt into the otherwise-dormant AutoCompactShaper via a sync bridge over
    the async LLM. We build a fresh client *inside* the summarizer coroutine so
    its async HTTP client never crosses event loops; an injected ``llm`` is
    reused as-is.
    """
    reasoning_effort = getattr(agent, "reasoning_effort", None)
    summary_extra = (
        {"reasoning_effort": reasoning_effort}
        if reasoning_effort is not None
        else {}
    )
    if llm is not None:

        async def _summary_complete(request: list[dict[str, Any]]) -> Any:
            return await resolved_llm.complete(
                request,
                temperature=0.0,
                **summary_extra,
            )
    else:

        async def _summary_complete(request: list[dict[str, Any]]) -> Any:
            client = LLMClient(
                model=agent.model,
                api_key=agent.api_key,
                base_url=agent.base_url,
                provider=agent.provider,
                wire_protocol=getattr(agent, "wire_protocol", "chat_completions"),
                max_retries=getattr(agent, "llm_max_retries", 3),
                request_timeout=llm_timeout,
                connect_timeout=getattr(agent, "llm_connect_timeout", 30.0),
                first_event_timeout=getattr(agent, "llm_first_event_timeout", 180.0),
                stream_idle_timeout=getattr(agent, "llm_stream_idle_timeout", 180.0),
                stream_chat=getattr(agent, "llm_stream_chat", False),
                context_window=getattr(agent, "context_window", None),
                provider_error_time_budget=getattr(agent, "provider_error_time_budget", 0.0),
                provider_retry_budget=provider_retry_budget,
            )
            primary_failure: BaseException | None = None
            try:
                return await client.complete(
                    request,
                    temperature=0.0,
                    **summary_extra,
                )
            except BaseException as exc:
                primary_failure = exc
                raise
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        outcome = close()
                        if inspect.isawaitable(outcome):
                            await outcome
                    except BaseException as close_failure:
                        if primary_failure is None:
                            raise
                        add_exception_note(
                            primary_failure,
                            f"summary client close also failed: {type(close_failure).__name__}: {close_failure}",
                        )

    return ReadTimeSummarizer(_summary_complete, transcript_path=auto_save_path)


def _build_default_shaper(
    resolved_llm: LLMPort, summarizer: ReadTimeSummarizer
) -> ShaperPort:
    """Assemble the default lazy-degradation shaper pipeline.

    Cheapest/lowest-loss first: per-tool-result budget bounds any one result;
    the reactive history layers then bound the *total* view once it crosses the
    trigger — clear old tool *content* in place (lowest loss) → snip whole old
    tool turns → auto-compact (summarize the remaining old span via the
    Claude Code-derived compaction prompt). All read-time over a copy; transcript stays full for lossless
    resume.

    Every layer here is pressure-triggered: each no-ops until an estimated
    context size crosses its trigger. ``EagerToolOutputClearShaper`` is
    deliberately not among them, although it was. It ran unconditionally on
    every call, and the two costs it carries are both paid at low context,
    where there is no pressure to relieve:

    * It rewrites history. Each call stubs the one result that has just aged
      past its keep-recent window, so the message at that depth differs from
      the message sent last call — the provider's prompt prefix stops matching
      there and the whole tail after it is re-read at full input price, every
      call. Append-only history matches to the end. The layers below rewrite
      too, but only once pressure is real, and against a window that is
      genuinely too large to send.
    * The model re-reads what was cleared. Runs on this repository's own tasks
      showed the same file re-read eight and nine times while the context peak
      sat at about two percent of the window, so the tokens the clearing saved
      were spent again on fetching the same bytes back.

    Turning it off hands its job to the layers below, which do the same work on
    the same messages when the context is actually large. It is not deleted:
    ``opencollab.application.shaping`` still exports it, and a caller that
    wants an always-on front rung can compose one.
    """
    # Trigger/target scale to the active model's real context window, degrading
    # to fixed defaults when the model is unrecognised.
    context_window = getattr(resolved_llm, "context_window", lambda: None)()
    history_trigger, history_target = history_trigger_target(context_window)

    # Inject the shaping module's own estimator rather than reaching past it:
    # the layers must size history the way the *request* is sized, provider
    # state included, or compaction fires late by exactly the thinking traces
    # it cannot see. Passing a different callable here silently overrode that.
    history_kwargs = {
        "estimate_tokens": approx_messages_tokens,
        "trigger_tokens": history_trigger,
        "target_tokens": history_target,
    }
    return ShaperPipeline(
        (
            PerToolResultBudgetShaper(DEFAULT_TOOL_RESULT_BUDGET),
            ToolOutputClearShaper(compactable_tools=COMPACTABLE_TOOL_NAMES, **history_kwargs),
            OldHistorySnipShaper(**history_kwargs),
            AutoCompactShaper(summarizer=summarizer, **history_kwargs),
        )
    )


def build_skill_store(workspace: str | None) -> SkillStorePort:
    """Resolve the workspace's ``skills/`` directory into a skill store.

    A repo opts into skills by adding a ``skills/`` directory at its root. When
    that directory exists we scan it with ``FileSkillStore``; otherwise (no
    workspace, or no ``skills/`` dir) we return a ``NullSkillStore`` so behavior
    is byte-identical to today — no catalog appears and no body resolves. This
    is the only place that picks the concrete store type.
    """
    if workspace:
        skills_root = Path(workspace) / "skills"
        if skills_root.is_dir():
            return FileSkillStore(skills_root)
    return NullSkillStore()


def build_session_runtime(
    *,
    agent: Agent,
    env: Environment | None = None,
    tracer: TracePort | None = None,
    max_budget_tokens: int = 1_000_000,
    max_steps: int = 100,
    auto_save_path: str | None = None,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    ask_policy: AskUserPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    llm_timeout: float = 600.0,
    provider_retry_budget: Any | None = None,
    store: SessionStorePort | None = None,
    auto_save_callback: Callable[[], None] | None = None,
    auto_save_prepare_callback: Callable[
        [], Callable[[], None] | None
    ] | None = None,
    aid: int = -1,
    seed_user_messages: list[dict[str, Any]] | None = None,
    seed_system_messages: list[dict[str, Any]] | None = None,
    shaper: ShaperPort | None = None,
    team_budget_exhausted: Callable[[], bool] | None = None,
) -> SessionRuntime:
    """Build a ``SessionRuntime`` with the same construction order
    ``Session.__init__`` used to perform inline.

    ``auto_save_callback`` is the bound method the facade exposes for direct
    autosave. ``auto_save_prepare_callback`` freezes a payload on the event
    loop and returns the file-I/O operation run by the subscriber worker.
    ``seed_system_messages`` retain source-level provenance for layered system
    context; ``seed_user_messages`` are startup user-context messages appended
    after them (e.g. a spawned agent's task);
    ``shaper`` reshapes the message list before each model call.
    """
    resolved_env = env if env is not None else LocalEnvironment()
    resolved_store: SessionStorePort = store if store is not None else SessionStore()

    event_bus = EventBus(event_sink)
    auto_save_subscriber: AutoSaveSubscriber | None = None
    if auto_save_path and auto_save_callback is not None:
        auto_save_subscriber = AutoSaveSubscriber(
            auto_save_callback,
            prepare_fn=auto_save_prepare_callback,
        )
        event_bus.subscribe(auto_save_subscriber)

    state = _build_initial_state(
        agent,
        seed_user_messages,
        seed_system_messages,
    )
    state.aid = aid

    resolved_llm = _resolve_llm(agent, llm, llm_timeout, provider_retry_budget)

    tool_execution = ToolExecutionUseCase(
        agent=agent,
        environment=resolved_env,
        state=state,
        event_publisher=event_bus,
        tracer=tracer,
        permission_policy=permission_policy,
        ask_policy=ask_policy,
        safety_policy=safety_policy,
    )
    summarizer = _build_summarizer(
        agent,
        llm,
        resolved_llm,
        llm_timeout,
        auto_save_path,
        provider_retry_budget,
    )
    resolved_shaper: ShaperPort = (
        shaper if shaper is not None else _build_default_shaper(resolved_llm, summarizer)
    )
    runner = SessionRunUseCase(
        agent=agent,
        state=state,
        llm=resolved_llm,
        event_publisher=event_bus,
        tool_execution=tool_execution,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
        shaper=resolved_shaper,
        team_budget_exhausted=team_budget_exhausted,
        # The context-overflow classifier lives in the adapter layer; injected
        # as a plain callable so the application use case never imports it (same
        # boundary pattern as the team-budget predicate). Enables the
        # force-compact-and-retry + graceful-stop safety net in ``call_llm``.
        is_context_overflow=is_context_overflow_error,
        # Cooperative per-generation ceiling: bound a single model call to the
        # configured provider timeout so one slow (thinking) generation cannot
        # eat the whole run wall (P7).
        per_call_timeout=llm_timeout,
    )

    return SessionRuntime(
        state=state,
        event_bus=event_bus,
        llm=resolved_llm,
        store=resolved_store,
        tool_execution=tool_execution,
        runner=runner,
        auto_save_path=auto_save_path,
        auto_save_subscriber=auto_save_subscriber,
        owns_llm=llm is None,
    )


# ---------------------------------------------------------------------------
# Lazy re-exports of the higher-level factories.
#
# ``session_factory`` and ``scheduler_factory`` import names from THIS module,
# so importing them eagerly here would create an import cycle. PEP 562 module
# ``__getattr__`` defers their import until first attribute access, by which
# point this module is fully initialised — keeping load order acyclic no matter
# which module is imported first.
# ---------------------------------------------------------------------------

_SESSION_FACTORY_EXPORTS = frozenset(
    {
        "DefaultSessionFactory",
        "SnapshotSessionError",
        "agent_save_path",
        "build_session",
        "build_spawn_session",
        "load_session",
        "make_run_dir",
        "snapshot_session",
    }
)
_SCHEDULER_FACTORY_EXPORTS = frozenset({"build_scheduler"})


def __getattr__(name: str) -> Any:
    if name in _SESSION_FACTORY_EXPORTS:
        from opencollab.bootstrap import session_factory

        return getattr(session_factory, name)
    if name in _SCHEDULER_FACTORY_EXPORTS:
        from opencollab.bootstrap import scheduler_factory

        return getattr(scheduler_factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContextBuilder",
    "DefaultSessionFactory",
    "RuntimeContext",
    "SessionRuntime",
    "SnapshotSessionError",
    "SpawnConfig",
    "agent_save_path",
    "make_run_dir",
    "build_runtime_context",
    "build_session",
    "build_session_runtime",
    "build_skill_store",
    "build_scheduler",
    "build_spawn_session",
    "build_tools_for_role",
    "build_workspace_safety_policy",
    "load_session",
    "snapshot_session",
]
