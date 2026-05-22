from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from opencollab.application.compaction import ContextCompactionUseCase
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import EventPublisherPort, LLMPort, TracePort
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionPhase, SessionState


@dataclass
class PendingStep:
    """The LLM response carried across HANDLING_RESPONSE → EXECUTING_TOOLS →
    AUTOSAVING. Lives here, not in domain ``SessionState``: the response is an
    adapter-shaped object and would breach the inward dependency rule.
    """

    response: Any
    latency: float


class SessionRunUseCase:
    """Application use case for the session run loop.

    The LLM response is structural: it must expose ``content``,
    ``tool_calls``, ``finish_reason``, and ``usage.total_tokens``.
    """

    def __init__(
        self,
        *,
        agent: Any,
        state: SessionState,
        llm: LLMPort,
        event_publisher: EventPublisherPort,
        event_factory: SessionEventFactory | None = None,
        tool_execution: ToolExecutionUseCase,
        compaction: ContextCompactionUseCase,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
    ):
        self.agent = agent
        self.state = state
        self.llm = llm
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(state.aid)
        self.tool_execution = tool_execution
        self.compaction = compaction
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self._pending: PendingStep | None = None

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        try:
            # A completed turn (DONE) short-circuits to its last answer; an
            # aborted turn (cancelled/budget/error) is reset so a re-run resumes.
            if self.is_terminal_phase() and self.state.phase is not SessionPhase.DONE:
                self.state.transition_to(SessionPhase.IDLE)
            while not self.is_terminal_phase() and self.state.step_count < self.max_steps:
                await self.advance(cancel_event)

        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception:
            self.state.set_phase(SessionPhase.ERROR)
            raise

        for msg in reversed(self.state.messages):
            if msg["role"] == "assistant" and msg.get("content"):
                return msg["content"]
        return ""

    def is_terminal_phase(self) -> bool:
        return self.state.phase.is_terminal()

    async def advance(self, cancel_event: asyncio.Event | None = None) -> None:
        match self.state.phase:
            case SessionPhase.SCHEDULED:
                self.state.transition_to(SessionPhase.IDLE)
            case SessionPhase.IDLE:
                self.state.transition_to(SessionPhase.PRECHECK)
            case SessionPhase.PRECHECK:
                await self.precheck(cancel_event)
            case SessionPhase.COMPACTING:
                await self.run_compaction()
            case SessionPhase.CALLING_LLM:
                await self.run_llm_call()
            case SessionPhase.HANDLING_RESPONSE:
                await self.handle_pending_response()
            case SessionPhase.EXECUTING_TOOLS:
                await self.execute_pending_tools()
            case SessionPhase.AUTOSAVING:
                await self.autosave_pending_step()
            case _:
                self.state.set_phase(SessionPhase.ERROR)
                raise RuntimeError(f"Cannot advance terminal phase: {self.state.phase.value}")

    async def precheck(self, cancel_event: asyncio.Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            self.state.append_message({"role": "system", "content": "[Session interrupted by user]"})
            await self.event_publisher.emit(self.event_factory.error("cancelled"))
            self.state.transition_to(SessionPhase.CANCELLED)
            return

        if self.state.used_tokens >= self.max_budget_tokens:
            self.state.append_message(
                {
                    "role": "system",
                    "content": f"[Budget exceeded: {self.state.used_tokens} tokens used. Session stopped.]",
                }
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.transition_to(SessionPhase.BUDGET_EXCEEDED)
            return

        if self.compaction.should_compact():
            self.state.transition_to(SessionPhase.COMPACTING)
            return

        self.state.transition_to(SessionPhase.CALLING_LLM)

    async def run_compaction(self) -> None:
        result = await self.compaction.compact(apply=False)
        result.apply_to(self.state)
        if result.did_compact:
            await self.event_publisher.emit(
                self.event_factory.compaction_applied(self.state.used_tokens)
            )
        self.state.transition_to(SessionPhase.CALLING_LLM)

    async def run_llm_call(self) -> None:
        self.state.advance_step()
        await self.event_publisher.emit(self.event_factory.step_start(self.state.step_count))
        start = time.monotonic()

        tools = self.build_tool_schemas()
        response = await self.call_llm(tools)
        latency = time.monotonic() - start
        self.state.add_used_tokens(response.usage.total_tokens)

        self.record_llm_trace(response, latency)
        self.append_assistant_message(response)
        self._pending = PendingStep(response=response, latency=latency)
        self.state.transition_to(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        pending = self._pending
        if pending is None:
            self.state.set_phase(SessionPhase.ERROR)
            raise RuntimeError("Cannot handle assistant response before calling LLM")
        response = pending.response

        if response.content:
            await self.event_publisher.emit(self.event_factory.text_delta(response.content))

        if response.tool_calls:
            self.state.transition_to(SessionPhase.EXECUTING_TOOLS)
            return

        await self.finish_step(pending.latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.DONE)

    async def execute_pending_tools(self) -> None:
        pending = self._pending
        if pending is None:
            self.state.set_phase(SessionPhase.ERROR)
            raise RuntimeError("Cannot execute tools before calling LLM")

        result = await self.tool_execution.process(pending.response.tool_calls)
        result.apply_to(self.state)
        self.state.transition_to(SessionPhase.AUTOSAVING)

    async def autosave_pending_step(self) -> None:
        pending = self._pending
        latency = pending.latency if pending is not None else 0.0
        await self.finish_step(latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending = None

    def build_tool_schemas(self) -> list[dict] | None:
        return self.agent.tool_schemas() or None

    async def call_llm(self, tools: list[dict] | None) -> Any:
        return await self.llm.complete(
            messages=self.state.messages,
            tools=tools,
            temperature=self.agent.temperature,
        )

    def record_llm_trace(self, response: Any, latency: float) -> None:
        if self.tracer:
            tool_calls_log = None
            if response.tool_calls:
                tool_calls_log = [
                    {
                        "id": tc.get("id"),
                        "name": tc.get("function", {}).get("name"),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    }
                    for tc in response.tool_calls
                ]
            self.tracer.log_step(
                step_type="llm_call",
                payload={
                    "model": self.agent.model,
                    "finish_reason": response.finish_reason,
                    "content": response.content,
                    "tool_calls": tool_calls_log,
                },
                tokens=response.usage.total_tokens,
                latency=latency,
            )

    def append_assistant_message(self, response: Any) -> None:
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = response.tool_calls
        self.state.append_message(assistant_msg)

    async def finish_step(self, latency: float) -> None:
        # AutoSaveSubscriber listens for step_end on the bus.
        await self.event_publisher.emit(
            self.event_factory.step_end(self.state.step_count, latency)
        )
