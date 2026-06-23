from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    CompletionResponse,
    EventPublisherPort,
    LLMPort,
    ShaperPort,
    TracePort,
)
from opencollab.application.shaping import forced_shape
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState

logger = logging.getLogger(__name__)

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})


class _ContextOverflowStop(Exception):
    """Internal control-flow signal: the prompt still overflows the model window
    after a forced maximal compaction pass and one retry. Caught inside
    ``run_llm_call`` to perform a controlled graceful stop (CONTEXT_OVERFLOW)
    rather than letting it propagate as an unhandled ERROR. Never escapes the
    use case.
    """


@dataclass
class PendingStep:
    """The LLM response carried across HANDLING_RESPONSE → EXECUTING_TOOLS →
    AUTOSAVING. Lives here, not in domain ``SessionState``: the response is an
    adapter-shaped object and would breach the inward dependency rule.
    """

    response: CompletionResponse
    latency: float


# Re-prompt used when a turn returns neither text nor a tool call (an
# "empty-stop"). Without this the session would silently transition to DONE,
# recording a clean completion that produced no answer and took no action.
_EMPTY_STOP_NUDGE = (
    "Your previous response was empty — no message and no tool call. "
    "If the task is finished, say so briefly. Otherwise continue now by "
    "calling the appropriate tool or giving your answer."
)

# Stand-in for the empty assistant turn, inserted before the nudge so the retry
# request never sends two consecutive user messages (Anthropic rejects that).
# Filtered out of ``run_loop``'s answer scan so it is never mistaken for output.
_EMPTY_STOP_PLACEHOLDER = "[no output produced this turn]"


class SessionRunUseCase:
    """Application use case for the session run loop.

    The LLM response is structural (``CompletionResponse`` in
    ``application/ports.py``): it must expose ``content``, ``tool_calls``,
    ``finish_reason``, ``usage.input_tokens``, and ``usage.total_tokens``.
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
        tracer: TracePort | None = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
        deferrable_tool_names: frozenset[str] = DEFAULT_DEFERRABLE_TOOLS,
        shaper: ShaperPort | None = None,
        team_budget_exhausted: Callable[[], bool] | None = None,
        is_context_overflow: Callable[[Exception], bool] | None = None,
        per_call_timeout: float | None = None,
    ):
        self.agent = agent
        self.state = state
        self.llm = llm
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(state.aid)
        self.tool_execution = tool_execution
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self.deferrable_tool_names = deferrable_tool_names
        self.shaper = shaper
        # Defense-in-depth aggregate ceiling. An injected zero-arg predicate that
        # reports whether the *team total* spend has reached the global cap.
        # Passed as a plain callable (resolved in bootstrap/the factory) so the
        # application layer never imports the concrete Scheduler — same pattern as
        # ``max_budget_tokens`` being injected rather than read from the scheduler.
        # ``None`` for standalone (non-team) sessions: only the per-session cap
        # applies, preserving existing behavior.
        self._team_budget_exhausted = team_budget_exhausted
        # Adapter-supplied predicate classifying a provider exception as a
        # context-window overflow (prompt too large even after reactive
        # compaction). Injected as a plain callable (resolved in the factory) so
        # the application layer never imports ``adapters.llm`` — same boundary
        # pattern as ``team_budget_exhausted``. ``None`` (tests/standalone)
        # means "never an overflow", preserving the prior propagate-as-ERROR
        # behaviour for callers that don't wire it.
        self._is_context_overflow = is_context_overflow or (lambda _exc: False)
        # Hard ceiling (seconds) on a SINGLE model generation. The provider's own
        # httpx ``request_timeout`` does not bound a slow streaming/thinking
        # generation cooperatively; this wraps the call in ``asyncio.wait_for`` so
        # one ~595s generation cannot consume the whole run wall (P7). ``None``
        # disables it, preserving prior behavior for callers that don't wire it.
        self._per_call_timeout = per_call_timeout
        self._pending: PendingStep | None = None
        # Guards the once-per-session retry on an empty-stop turn (see
        # ``handle_pending_response``).
        self._empty_stop_retried = False

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        """Drive the phase FSM until the turn finishes or suspends.

        Returns the last assistant text answer (empty string if none). On an
        unexpected exception the session is marked failed and the exception
        re-raised; cancellation flushes the tracer before propagating.
        """
        try:
            # A session suspended on deferred work resumes from its pending
            # table; a completed turn (DONE) short-circuits to its last answer;
            # an aborted turn (cancelled/budget/error) resumes to IDLE so a bare
            # re-run continues. resume_to_idle no-ops on non-terminal phases.
            if self.state.phase is SessionPhase.AWAITING_EVENTS:
                self._resume_from_awaiting()
            elif self.state.phase is not SessionPhase.DONE:
                self.state.resume_to_idle()
                # A fresh turn (or re-run) restores the once-per-turn empty-stop
                # retry budget. A deferred-resume (AWAITING_EVENTS, above) is the
                # same turn and must keep the flag as-is.
                self._empty_stop_retried = False
            while not self._should_suspend():
                await self.advance(cancel_event)

        except asyncio.CancelledError:
            if self.tracer:
                self.tracer.flush()
            raise
        except Exception as exc:
            self.state.fail(reason=f"{type(exc).__name__}: {exc}")
            raise

        for msg in reversed(self.state.messages):
            content = msg.get("content")
            if msg["role"] == "assistant" and content and content != _EMPTY_STOP_PLACEHOLDER:
                return content
        return ""

    def is_terminal_phase(self) -> bool:
        """Whether the session has finished its turn (done/failed/limits)."""
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
        """Execute one FSM step: dispatch the current phase to its handler."""
        match self.state.phase:
            case SessionPhase.SCHEDULED:
                self.state.transition_to(SessionPhase.IDLE)
            case SessionPhase.IDLE:
                self.state.transition_to(SessionPhase.PRECHECK)
            case SessionPhase.PRECHECK:
                await self.precheck(cancel_event)
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
        """Gate the next LLM call: cancellation, token budget, step limit.

        Each guard appends a visible system message, emits an error event, and
        moves to the matching terminal phase; otherwise proceed to CALLING_LLM.
        """
        if cancel_event and cancel_event.is_set():
            self.state.append_message({"role": "system", "content": "[Session interrupted by user]"})
            await self.event_publisher.emit(self.event_factory.error("cancelled"))
            self.state.transition_to(SessionPhase.CANCELLED, reason="interrupted by user")
            return

        if self.state.used_tokens >= self.max_budget_tokens:
            reason = f"budget exceeded: {self.state.used_tokens} tokens used"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.transition_to(SessionPhase.BUDGET_EXCEEDED, reason=reason)
            return

        # Aggregate ceiling (defense-in-depth): even when this session is under
        # its own cap, stop if the *team total* has reached the global cap. A
        # single overshooting turn or fan-out could otherwise spend past the
        # global pool that reserve-at-allocation is meant to protect.
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            reason = "team budget exceeded: aggregate spend reached the global cap"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("budget_exceeded"))
            self.state.transition_to(SessionPhase.BUDGET_EXCEEDED, reason=reason)
            return

        if self.state.step_count >= self.max_steps:
            reason = f"step limit reached: {self.state.step_count} steps"
            self.state.append_message(
                {"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"}
            )
            await self.event_publisher.emit(self.event_factory.error("step_limit_exceeded"))
            self.state.transition_to(SessionPhase.STEP_LIMIT_EXCEEDED, reason=reason)
            return

        self.state.transition_to(SessionPhase.CALLING_LLM)

    async def run_llm_call(self) -> None:
        """One model call: track tokens, trace, append the assistant message,
        and stash the response as the pending step for HANDLING_RESPONSE.

        Context-overflow safety net: ``call_llm`` already force-compacts and
        retries once on an overflow rejection. If the retry still overflows it
        raises ``_ContextOverflowStop``; we catch it here and stop the session
        gracefully (CONTEXT_OVERFLOW) instead of letting it crash as an
        unhandled ERROR — mirroring the BUDGET_EXCEEDED degradation.
        """
        self.state.advance_step()
        await self.event_publisher.emit(self.event_factory.step_start(self.state.step_count))
        start = time.monotonic()

        tools = self.build_tool_schemas()
        try:
            response = await self.call_llm(tools)
        except _ContextOverflowStop:
            await self._stop_on_context_overflow()
            return
        latency = time.monotonic() - start
        self.state.add_used_tokens(response.usage.total_tokens)
        self.state.set_context_tokens(response.usage.input_tokens)

        self.record_llm_trace(response, latency)
        self.append_assistant_message(response)
        self._pending = PendingStep(response=response, latency=latency)
        self.state.transition_to(SessionPhase.HANDLING_RESPONSE)

    async def handle_pending_response(self) -> None:
        """Route the pending LLM response: tool calls → EXECUTING_TOOLS;
        a plain text answer finishes the turn (DONE)."""
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

        # Empty-stop: a clean ``stop`` turn that produced neither text nor a tool
        # call. Falling straight through to DONE would silently record a clean
        # completion that answered nothing. Retry once with a nudge before giving
        # up; the once-per-turn flag (plus the budget/step limits) bounds it. The
        # AUTOSAVING handler finishes the step and loops back to PRECHECK.
        # ``finish_reason`` is gated to "stop": a "length" truncation will only
        # truncate again, so a nudge cannot help there.
        empty_stop = not response.content and not response.tool_calls
        if empty_stop and response.finish_reason in (None, "stop") and not self._empty_stop_retried:
            self._empty_stop_retried = True
            # Record the retry to the trajectory so empty-stops are measurable
            # (the injected nudge/placeholder messages are never persisted).
            if self.tracer:
                self.tracer.log_step(
                    step_type="empty_stop_retry",
                    payload={
                        "finish_reason": response.finish_reason,
                        "had_reasoning": bool(getattr(response, "reasoning", None)),
                    },
                    latency=pending.latency,
                )
            # The empty assistant turn was not appended, so the history may end
            # with a user/tool message. Insert a short assistant placeholder
            # before the nudge so the retry request never sends two consecutive
            # user messages (which Anthropic rejects).
            if self.state.messages and self.state.messages[-1]["role"] in ("user", "tool"):
                self.state.append_message(
                    {"role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER}
                )
            self.state.append_message({"role": "user", "content": _EMPTY_STOP_NUDGE})
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        await self.finish_step(pending.latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.DONE, reason="completed")

    async def execute_pending_tools(self) -> None:
        """Run the pending response's tool calls.

        All-synchronous batches run immediately and proceed to AUTOSAVING. A
        batch containing a deferrable tool (e.g. ``spawn_agent``) is buffered
        whole into the pending-events table so the eventual tool-result block
        stays contiguous; the session then suspends on AWAITING_EVENTS until
        the scheduler delivers the outstanding results.
        """
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
        """Partition a batch into (immediate, deferred) by deferrable name."""
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
        """Emit step_end (the autosave trigger) and loop back to PRECHECK."""
        pending = self._pending
        latency = pending.latency if pending is not None else 0.0
        await self.finish_step(latency)
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending = None

    def build_tool_schemas(self) -> list[dict] | None:
        return self.agent.tool_schemas() or None

    async def call_llm(self, tools: list[dict] | None) -> CompletionResponse:
        """Complete against the shaped view of history.

        The shaper reshapes a copy for the model's view only;
        ``state.messages`` stays the complete, persisted history.

        Context-overflow safety net: the normal shaped call uses the estimate-
        gated reactive layers, which can under-count dense content (code / JSON /
        CJK) and let a prompt overflow the real window. If the provider rejects
        the call as a context overflow, run a FORCED maximal compaction pass
        (compact every sheddable source unconditionally toward target,
        regardless of the estimate) and retry ONCE. If that *still* overflows —
        e.g. the pinned identity/team/task seed alone exceeds the window — raise
        ``_ContextOverflowStop`` so the caller stops the session gracefully.
        """
        messages = (
            self.shaper.shape(self.state.messages)
            if self.shaper is not None
            else self.state.messages
        )
        try:
            return await self._complete(messages, tools)
        except Exception as exc:
            if not self._is_context_overflow(exc):
                raise

        # First overflow: force a maximal compaction pass and retry once.
        forced = (
            forced_shape(self.shaper, self.state.messages)
            if self.shaper is not None
            else self.state.messages
        )
        logger.warning(
            "context overflow on aid=%s: recompacting (%d -> %d messages) and retrying once",
            self.state.aid,
            len(messages),
            len(forced),
        )
        await self.event_publisher.emit(self.event_factory.error("context_overflow_recompacted"))
        try:
            return await self._complete(forced, tools)
        except Exception as exc:
            if self._is_context_overflow(exc):
                raise _ContextOverflowStop() from exc
            raise

    async def _complete(
        self, messages: list[dict], tools: list[dict] | None
    ) -> CompletionResponse:
        # ``tool_choice`` is read defensively (getattr) so duck-typed agent stubs
        # without the field keep working; ``None`` (the default) keeps the
        # provider's "auto", so the surface is unchanged for ordinary agents.
        # When an agent requests "required" (the forced-write coder) but the
        # provider rejects it (some OpenAI-compatible endpoints don't support
        # it), fall back ONCE to "auto" — the explicit forced prompt and the
        # diff-gate still push the model to write.
        tool_choice = getattr(self.agent, "tool_choice", None)
        try:
            return await self._complete_with_choice(messages, tools, tool_choice)
        except Exception as exc:
            if tool_choice in (None, "auto") or self._is_context_overflow(exc):
                raise
            # Only degrade to "auto" when the provider plainly REJECTED the
            # parameter — a 4xx request-validation error, or a message naming
            # tool_choice. A transient/auth/server error would fail "auto"
            # identically, so re-raise it instead of masking a real fault behind
            # a second call. Duck-typed so the application layer needs no
            # provider-specific exception imports.
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            msg = str(exc).lower()
            if status != 400 and "tool_choice" not in msg and "tool choice" not in msg:
                raise
            logger.warning(
                "tool_choice=%r rejected on aid=%s (%s); retrying once with 'auto'",
                tool_choice,
                self.state.aid,
                type(exc).__name__,
            )
            return await self._complete_with_choice(messages, tools, "auto")

    async def _complete_with_choice(
        self, messages: list[dict], tools: list[dict] | None, tool_choice: str | None
    ) -> CompletionResponse:
        # ``thinking`` is read defensively (getattr) so duck-typed agent stubs
        # without the field keep working. When OFF (the default) the call is made
        # exactly as before — the thinking kwargs are omitted entirely so the LLM
        # surface is byte-for-byte unchanged for every existing caller.
        #
        # ``tool_choice`` is included ONLY when set: an ordinary agent
        # (tool_choice=None) calls ``complete`` with exactly the prior kwargs, so
        # duck-typed LLM stubs that never added the param keep working; only a
        # forcing agent passes it through.
        extra: dict[str, Any] = {}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        if not getattr(self.agent, "thinking", False):
            return await self._invoke_llm(
                messages=messages,
                tools=tools,
                temperature=self.agent.temperature,
                **extra,
            )
        return await self._invoke_llm(
            messages=messages,
            tools=tools,
            temperature=self.agent.temperature,
            thinking=True,
            thinking_params=getattr(self.agent, "thinking_params", None),
            **extra,
        )

    async def _invoke_llm(self, **kwargs: Any) -> CompletionResponse:
        """Call the provider, bounding a single generation by ``_per_call_timeout``.

        Without a per-call ceiling one slow generation (a 595s thinking turn was
        observed) can consume the entire run wall, so the outer
        ``asyncio.wait_for`` on the whole workflow truncates everything before any
        patch lands. ``asyncio.wait_for`` cancels the in-flight ``complete`` call
        on expiry; the resulting ``TimeoutError`` propagates and is handled by the
        workflow/agent wrapper as a dead step (the run continues with whatever is
        already in the working tree). ``None`` disables the ceiling.
        """
        if self._per_call_timeout is None:
            return await self.llm.complete(**kwargs)
        return await asyncio.wait_for(
            self.llm.complete(**kwargs), timeout=self._per_call_timeout
        )

    async def _stop_on_context_overflow(self) -> None:
        """Graceful terminal stop when a prompt overflows the model window even
        after forced compaction + one retry. Mirrors the BUDGET_EXCEEDED
        degradation: a visible system message, an error event, and a validated
        transition to the dedicated CONTEXT_OVERFLOW terminal. A child stopped
        this way delivers a controlled result to its parent (DONE row) rather
        than crashing the parent's turn.
        """
        reason = "context overflow: prompt exceeds the model context window even after compaction"
        self.state.append_message(
            {
                "role": "system",
                "content": (
                    "[Context overflow: prompt exceeds the model context window "
                    "even after compaction. Session stopped.]"
                ),
            }
        )
        await self.event_publisher.emit(self.event_factory.error("context_overflow"))
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.CONTEXT_OVERFLOW, reason=reason)

    def record_llm_trace(self, response: CompletionResponse, latency: float) -> None:
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
            payload = {
                "model": self.agent.model,
                "finish_reason": response.finish_reason,
                "content": response.content,
                "tool_calls": tool_calls_log,
            }
            # Record provider chain-of-thought to the trajectory when present
            # (omitted otherwise, so non-thinking traces keep their prior shape).
            reasoning = getattr(response, "reasoning", None)
            if reasoning:
                payload["reasoning"] = reasoning
            self.tracer.log_step(
                step_type="llm_call",
                payload=payload,
                tokens=response.usage.total_tokens,
                latency=latency,
            )

    def append_assistant_message(self, response: CompletionResponse) -> None:
        # An empty-stop turn (no content, no tool calls) would append a bare
        # ``{"role": "assistant"}`` message that some providers reject on the
        # next request. Skip it — handle_pending_response decides retry-vs-DONE.
        if not response.content and not response.tool_calls:
            return
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
