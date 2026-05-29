from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from opencollab.application.compaction import ContextCompactionUseCase
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import EventPublisherPort, LLMPort, TracePort
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})


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
    ``tool_calls``, ``finish_reason``, ``usage.input_tokens``, and
    ``usage.total_tokens``.
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
        deferrable_tool_names: frozenset[str] = DEFAULT_DEFERRABLE_TOOLS,
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
        self.deferrable_tool_names = deferrable_tool_names
        self._pending: PendingStep | None = None

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        try:
            # A session suspended on deferred work resumes from its pending
            # table; a completed turn (DONE) short-circuits to its last answer;
            # an aborted turn (cancelled/budget/error) resumes to IDLE so a bare
            # re-run continues. resume_to_idle no-ops on non-terminal phases.
            if self.state.phase is SessionPhase.AWAITING_EVENTS:
                self._resume_from_awaiting()
            elif self.state.phase is not SessionPhase.DONE:
                self.state.resume_to_idle()
            while not self._should_suspend():
                await self.advance(cancel_event)

        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception:
            self.state.fail()
            raise

        for msg in reversed(self.state.messages):
            if msg["role"] == "assistant" and msg.get("content"):
                return msg["content"]
        return ""

    def is_terminal_phase(self) -> bool:
        return self.state.phase.is_terminal()

    def _should_suspend(self) -> bool:
        """The loop stops on a terminal phase (turn finished) OR on the
        non-terminal AWAITING_EVENTS suspend state (turn paused on deferred
        work; the scheduler re-activates it when the events arrive).
        """
        return self.is_terminal_phase() or self.state.phase is SessionPhase.AWAITING_EVENTS

    def _resume_from_awaiting(self) -> None:
        """Drain a complete pending table back into the message history as one
        contiguous tool-result block, then resume at PRECHECK. Defensive no-op
        if woken while still incomplete (the loop guard then stops at once).
        """
        table = self.state.pending_events
        if not table.is_complete():
            return
        for message in table.ordered_results():
            self.state.append_message(message)
        table.clear()
        self.state.transition_to(SessionPhase.PRECHECK)

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
            case SessionPhase.AWAITING_EVENTS:
                # Never dispatched: the loop guard suspends before reaching here.
                self.state.fail()
                raise RuntimeError("advance() called while AWAITING_EVENTS")
            case SessionPhase.AUTOSAVING:
                await self.autosave_pending_step()
            case _:
                self.state.fail()
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

        if self.state.step_count >= self.max_steps:
            self.state.append_message(
                {
                    "role": "system",
                    "content": f"[Step limit reached: {self.state.step_count} steps. Session stopped.]",
                }
            )
            await self.event_publisher.emit(self.event_factory.error("step_limit_exceeded"))
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
        self.state.set_context_tokens(response.usage.input_tokens)

        self.record_llm_trace(response, latency)
        self.append_assistant_message(response)
        self._pending = PendingStep(response=response, latency=latency)
        self.state.transition_to(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        pending = self._pending
        if pending is None:
            self.state.fail()
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
            self.state.fail()
            raise RuntimeError("Cannot execute tools before calling LLM")

        tool_calls = pending.response.tool_calls
        immediate, deferred = self._split_tool_calls(tool_calls)

        # Fast path, unchanged: every tool is synchronous — run them, append
        # results, and autosave the step.
        if not deferred:
            result = await self.tool_execution.process(tool_calls)
            result.apply_to(self.state)
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        # A deferred tool is present. Buffer the WHOLE batch into the pending
        # table so the eventual tool-result block stays contiguous and in the
        # original order, satisfying the LLM rule that every tool_call_id is
        # answered before the next model call.
        table = self.state.pending_events
        order = {tc["id"]: i for i, tc in enumerate(tool_calls)}

        if immediate:
            proc = await self.tool_execution.process(immediate)
            proc.apply_hashes_to(self.state)  # hashes now; messages buffered
            for message in proc.messages_to_append:
                tid = message["tool_call_id"]
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.IMMEDIATE,
                        order=order[tid],
                        status=RowStatus.DONE,
                        result=message["content"],
                    )
                )

        for tc in deferred:
            tid = tc["id"]
            ref, error = await self.tool_execution.execute_deferred(tc)
            if ref is not None:
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.CHILD_AGENT,
                        order=order[tid],
                        ref=ref,
                        status=RowStatus.PENDING,
                    )
                )
            else:
                table.add(
                    PendingRow(
                        tool_call_id=tid,
                        kind=RowKind.CHILD_AGENT,
                        order=order[tid],
                        status=RowStatus.FAILED,
                        result=error,
                        error=error,
                    )
                )

        if table.is_complete():
            # Nothing is actually outstanding (e.g. all spawns were rejected
            # synchronously) — drain now and autosave instead of suspending on
            # an event that will never arrive.
            for message in table.ordered_results():
                self.state.append_message(message)
            table.clear()
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        # Genuinely waiting on ≥1 child. Suspend WITHOUT finishing the step:
        # emitting step_end here would autosave a half-open tool-call block (no
        # results yet). The next AUTOSAVING after resume persists a consistent
        # history. tool_start/tool_end + the scheduler's agent_spawned still
        # provide observability of the spawn.
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.AWAITING_EVENTS)

    def _split_tool_calls(self, tool_calls: list[dict]) -> tuple[list[dict], list[dict]]:
        immediate: list[dict] = []
        deferred: list[dict] = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name")
            if name in self.deferrable_tool_names:
                deferred.append(tc)
            else:
                immediate.append(tc)
        return immediate, deferred

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
