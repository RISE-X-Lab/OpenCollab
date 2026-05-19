from __future__ import annotations

import asyncio
from typing import Any

from opencollab.application.session_run import SessionRunEventFactory, SessionRunUseCase
from opencollab.core.session.compactor import ContextCompactor
from opencollab.core.session.events import EventBus, SessionEvent
from opencollab.core.session.tools import ToolCallProcessor
from opencollab.domain.session import SessionState


class SessionRunner:
    def __init__(
        self,
        *,
        agent: Any,
        state: SessionState,
        llm: Any,
        event_bus: EventBus,
        tool_processor: ToolCallProcessor,
        compactor: ContextCompactor,
        tracer: Any = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
    ):
        self.agent = agent
        self.state = state
        self.llm = llm
        self.event_bus = event_bus
        self.tool_processor = tool_processor
        self.compactor = compactor
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps

        self._use_case = SessionRunUseCase(
            agent=self.agent,
            state=self.state,
            llm=self.llm,
            event_publisher=self.event_bus,
            event_factory=self._event_factory(),
            tool_execution=getattr(self.tool_processor, "_tool_execution", self.tool_processor),
            compaction=getattr(self.compactor, "_use_case", self.compactor),
            tracer=self.tracer,
            max_budget_tokens=self.max_budget_tokens,
            max_steps=self.max_steps,
        )

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        return await self._use_case.run_loop(cancel_event)

    def _event_factory(self) -> SessionRunEventFactory:
        return SessionRunEventFactory(
            step_start=lambda step: SessionEvent(type="step_start", data={"step": step}),
            step_end=lambda step, latency: SessionEvent(
                type="step_end",
                data={"step": step, "latency": latency},
            ),
            text_delta=lambda content: SessionEvent(
                type="text_delta",
                data={"content": content},
            ),
            error=lambda reason: SessionEvent(
                type="error",
                data={"reason": reason},
            ),
            compaction_applied=lambda tokens_after: SessionEvent(
                type="compaction_applied",
                data={"tokens_after": tokens_after},
            ),
        )
