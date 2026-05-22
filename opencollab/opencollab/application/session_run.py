from __future__ import annotations

import asyncio
import time
from typing import Any

from opencollab.application.compaction import ContextCompactionUseCase
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import EventPublisherPort, LLMPort, TracePort
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionPhase, SessionState


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
        self._pending_response: Any | None = None
        self._pending_latency: float = 0.0

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        try:
            self.state.set_phase(SessionPhase.IDLE)
            while (
                not self.state.is_done
                and not self.is_terminal_phase()
                and self.state.step_count < self.max_steps
            ):
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
        return self.state.phase in {
            SessionPhase.DONE,
            SessionPhase.CANCELLED,
            SessionPhase.BUDGET_EXCEEDED,
            SessionPhase.ERROR,
        }

    async def advance(self, cancel_event: asyncio.Event | None = None) -> None:
        match self.state.phase:
            case SessionPhase.IDLE:
                self.state.set_phase(SessionPhase.PRECHECK)
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
            self.state.set_phase(SessionPhase.CANCELLED)
            return

        if self.state.used_tokens >= self.max_budget_tokens:
            self.state.append_message(
                {
                    "role": "system",
                    "content": f"[Budget exceeded: {self.state.used_tokens} tokens used. Session stopped.]",
                }
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.set_phase(SessionPhase.BUDGET_EXCEEDED)
            return

        if self.compaction.should_compact():
            self.state.set_phase(SessionPhase.COMPACTING)
            return

        self.state.set_phase(SessionPhase.CALLING_LLM)

    async def run_compaction(self) -> None:
        result = await self.compaction.compact(apply=False)
        result.apply_to(self.state)
        if result.did_compact:
            await self.event_publisher.emit(
                self.event_factory.compaction_applied(self.state.used_tokens)
            )
        self.state.set_phase(SessionPhase.CALLING_LLM)

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
        self._pending_response = response
        self._pending_latency = latency
        self.state.set_phase(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        response = self._pending_response
        if response is None:
            self.state.set_phase(SessionPhase.ERROR)
            raise RuntimeError("Cannot handle assistant response before calling LLM")

        if response.content:
            await self.event_publisher.emit(self.event_factory.text_delta(response.content))

        if response.tool_calls:
            self.state.set_phase(SessionPhase.EXECUTING_TOOLS)
            return

        self.state.mark_done()
        await self.finish_step(self._pending_latency)
        self.clear_pending_step()
        self.state.set_phase(SessionPhase.DONE)

    async def execute_pending_tools(self) -> None:
        response = self._pending_response
        if response is None:
            self.state.set_phase(SessionPhase.ERROR)
            raise RuntimeError("Cannot execute tools before calling LLM")

        result = await self.tool_execution.process(response.tool_calls)
        result.apply_to(self.state)
        self.state.set_phase(SessionPhase.AUTOSAVING)

    async def autosave_pending_step(self) -> None:
        await self.finish_step(self._pending_latency)
        self.clear_pending_step()
        self.state.set_phase(SessionPhase.DONE if self.state.is_done else SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending_response = None
        self._pending_latency = 0.0

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
