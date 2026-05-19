"""Composition root for the session runtime.

Owns the concrete construction order of every collaborator a ``Session``
needs: ``EventBus``, ``SessionState``, ``SessionStore``, ``AutoSaveSubscriber``,
``LLMClient``, ``ToolCallProcessor``, ``ContextCompactor``, ``SessionRunner``.

The ``Session`` facade (``core.session.session.Session``) delegates here
when it isn't handed an already-built ``SessionRuntime`` bundle; bootstrap
factories may build the bundle explicitly when they want to swap a
collaborator (LLM, store, ...) without going through the Session
constructor's kwargs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from opencollab.application.ports import (
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SessionStorePort,
    TracePort,
)
from opencollab.application.event_bus import EventBus, EventSink
from opencollab.domain.agent import Agent
from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.adapters.llm import LLMClient
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.core.session.compactor import (
    DEFAULT_COMPACTION_THRESHOLD,
    ContextCompactor,
)
from opencollab.core.session.runner import SessionRunner
from opencollab.adapters.storage import SessionStore
from opencollab.core.session.tools import ToolCallProcessor
from opencollab.domain.session import SessionState


@dataclass
class SessionRuntime:
    """Pre-built collaborators a ``Session`` facade keeps as attributes.

    Not frozen: ``Session`` reassigns a few attributes during lifecycle
    operations (e.g. setting permission policy propagates through the
    tool processor); keeping this mutable keeps the facade simple.
    """

    state: SessionState
    event_bus: EventBus
    llm: LLMPort
    store: SessionStorePort
    tool_processor: ToolCallProcessor
    compactor: ContextCompactor
    runner: SessionRunner
    auto_save_path: str | None


def _build_initial_state(agent: Agent, repo_map: str | None) -> SessionState:
    system_content = agent.system_prompt
    if repo_map:
        system_content += f"\n\nProject Structure:\n{repo_map}"
    return SessionState(messages=[{"role": "system", "content": system_content}])


def build_session_runtime(
    *,
    agent: Agent,
    env: Environment | None = None,
    tracer: TracePort | None = None,
    max_budget_tokens: int = 200_000,
    max_steps: int = 100,
    compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    repo_map: str | None = None,
    auto_save_path: str | None = None,
    event_sink: EventSink | None = None,
    permission_policy: PermissionPort | None = None,
    safety_policy: SafetyPolicyPort | None = None,
    llm: LLMPort | None = None,
    store: SessionStorePort | None = None,
    auto_save_callback: Callable[[], None] | None = None,
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

    state = _build_initial_state(agent, repo_map)

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

    tool_processor = ToolCallProcessor(
        agent=agent,
        env=resolved_env,
        state=state,
        event_bus=event_bus,
        tracer=tracer,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
    )
    compactor = ContextCompactor(
        state=state,
        llm=resolved_llm,
        event_bus=event_bus,
        tracer=tracer,
        compaction_threshold=compaction_threshold,
    )
    runner = SessionRunner(
        agent=agent,
        state=state,
        llm=resolved_llm,
        event_bus=event_bus,
        tool_processor=tool_processor,
        compactor=compactor,
        tracer=tracer,
        max_budget_tokens=max_budget_tokens,
        max_steps=max_steps,
    )

    return SessionRuntime(
        state=state,
        event_bus=event_bus,
        llm=resolved_llm,
        store=resolved_store,
        tool_processor=tool_processor,
        compactor=compactor,
        runner=runner,
        auto_save_path=auto_save_path,
    )


__all__ = ["SessionRuntime", "build_session_runtime"]
