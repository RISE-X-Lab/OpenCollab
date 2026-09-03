"""Provider completion and post-response helpers for session execution."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import Any

from opencollab.application._session_run_shared import (
    _STRUCTURED_OUTPUT_TOOL,
    _WRITE_TOOLS,
    GenerationTimeoutError,
    _ContextOverflowStop,
    _submit_tool_choice,
    _TokenBudgetStop,
)
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.ports import CompletionResponse
from opencollab.application.shaping import ShaperPipeline, forced_shape
from opencollab.application.steering import (
    READS_NUDGE_SOFT,
    build_steering_block,
    fold_steering,
    resolve_budget_nudge_mode,
    resolve_write_nudge_mode,
)
from opencollab.application.tool_execution import TERMINAL_CAPTURE_SKIP_MESSAGE
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP
from opencollab.domain.pending import PendingEventTable, PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_tokens
from opencollab.domain.tools import ToolProcessingResult

logger = logging.getLogger(__name__)


def _selector_names_tool_choice(selector: Any) -> bool:
    """Return whether a structured error selector targets ``tool_choice``."""
    values = selector if isinstance(selector, (list, tuple)) else [selector]
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
        segments = re.split(r"[.\[\]/]+", normalized)
        if "tool_choice" in segments:
            return True
    return False


def _selector_has_named_segment(selector: Any) -> bool:
    """Return whether a structured selector contains an authoritative name."""
    values = selector if isinstance(selector, (list, tuple)) else [selector]
    return any(isinstance(value, str) and bool(value.strip()) for value in values)


def _is_tool_choice_rejection(exc: Exception) -> bool:
    """Return whether a provider validation error specifically rejects choice."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None and status not in {400, 422}:
        return False

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            selector_keys = ("param", "field", "path")
            selectors = [
                detail[key]
                for key in selector_keys
                if key in detail and _selector_has_named_segment(detail[key])
            ]
            if selectors:
                return any(_selector_names_tool_choice(value) for value in selectors)
            code = detail.get("code")
            if code in {"invalid_tool_choice", "unsupported_tool_choice"}:
                return True

    message = str(exc).lower()
    choice = r"tool(?:_| )choice"
    rejection = (
        r"invalid|unsupported|not\s+supported|not\s+allowed|unknown|"
        r"unrecognized|unexpected|rejected"
    )
    return any(
        re.search(pattern, message)
        for pattern in (
            rf"\b(?:{rejection})\s+(?:(?:value\s+for|parameter|field)\s*:?\s+)?"
            rf"{choice}\b",
            rf"\b{choice}\b(?:\s+(?:parameter|field|value))?"
            rf"\s+(?:(?:is|was)\s+)?(?:{rejection})\b",
            rf"\b(?:does\s+not\s+support|doesn't\s+support|rejects?|rejected)"
            rf"\s+(?:the\s+)?{choice}\b",
        )
    )


class _SessionRunCompletionMixin:
    """Implementation details composed into ``SessionRunUseCase``."""

    async def _execute_deferred_tools(
        self,
        table: PendingEventTable,
        order: dict[str, int],
        tool_calls: list[dict],
    ) -> None:
        for tool_call in tool_calls:
            tool_call_id = tool_call["id"]
            # Register before the child starts so an immediate completion has a
            # row to fill while execute_deferred is still emitting its event.
            table.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.CHILD_AGENT,
                    order=order[tool_call_id],
                    status=RowStatus.PENDING,
                )
            )
            ref, error = await self.tool_execution.execute_deferred(tool_call)
            if ref is not None:
                table.rows[tool_call_id] = replace(table.rows[tool_call_id], ref=ref)
            else:
                table.fill(
                    tool_call_id,
                    status=RowStatus.FAILED,
                    result=error or "Deferred tool failed without an error message.",
                    error=error,
                )

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

    def _record_submission(self, result: Any) -> None:
        """Remember that this step called ``submit``, and with what."""
        if getattr(result, "turn_submitted", False):
            self._submitted_summary = getattr(result, "submitted_summary", None) or ""

    async def autosave_pending_step(self) -> None:
        """Emit step_end (the autosave trigger), then PRECHECK -- or DONE.

        DONE when the step that just ran called ``submit``. The summary the
        model gave it is appended as this turn's assistant answer first, so a
        submitted turn returns the agent's own account of what it handed over
        rather than whatever text happened to precede the tool call. Everything
        before the branch is unchanged, so a submitted step is saved exactly
        like any other.
        """
        pending = self._pending
        latency = (
            pending.latency
            if pending is not None
            else (self.state.pending_step_latency or 0.0)
        )
        submitted = self._submitted_summary
        if submitted is not None:
            self.state.append_message({"role": "assistant", "content": submitted})
        await self.finish_step(latency)
        self.clear_pending_step()
        if submitted is not None:
            self._submitted_summary = None
            self.state.transition_to(SessionPhase.DONE, reason="submitted")
            return
        self.state.transition_to(SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending = None
        self.state.pending_step_latency = None

    def build_tool_schemas(self) -> list[dict] | None:
        return self.agent.tool_schemas() or None

    def _write_only_tool_schemas(self, tools: list[dict] | None) -> list[dict] | None:
        return self._tool_schemas_with_names(tools, _WRITE_TOOLS)

    def _tool_schemas_with_names(self, tools: list[dict] | None, names: frozenset[str]) -> list[dict] | None:
        if not tools:
            return tools
        filtered = [spec for spec in tools if spec.get("function", {}).get("name") in names]
        return filtered or tools

    def _hard_steering_tools(
        self, tool_choice_override: Any | None
    ) -> tuple[frozenset[str], str]:
        tool_names = {getattr(t, "name", None) for t in getattr(self.agent, "tools", []) or []}
        if tool_choice_override == _submit_tool_choice(_STRUCTURED_OUTPUT_TOOL):
            return frozenset({_STRUCTURED_OUTPUT_TOOL}), "hard structured-output gate"
        if tool_names & _WRITE_TOOLS:
            return _WRITE_TOOLS, "hard write gate"
        if _STRUCTURED_OUTPUT_TOOL in tool_names:
            return frozenset({_STRUCTURED_OUTPUT_TOOL}), "hard structured-output gate"
        return frozenset(), "hard tool gate"

    def _apply_pending_tool_allowlist(
        self,
        tool_calls: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        allowed = self._pending_tool_allowlist
        if allowed is None:
            return tool_calls, []

        kept: list[dict] = []
        blocked: list[dict] = []
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name")
            if name in allowed:
                kept.append(tc)
                continue
            allowed_list = ", ".join(sorted(allowed))
            gate_label = self._pending_tool_gate_label or "hard tool gate"
            blocked.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": (
                        f"Error: tool '{name}' is not allowed during the {gate_label}. Use one of: {allowed_list}."
                    ),
                }
            )
        return kept, blocked

    def _ordered_tool_messages(
        self,
        tool_calls: list[dict],
        *message_groups: list[dict],
    ) -> list[dict]:
        by_id: dict[str, dict] = {}
        extras: list[dict] = []
        for group in message_groups:
            for message in group:
                tool_call_id = message.get("tool_call_id")
                if isinstance(tool_call_id, str):
                    by_id[tool_call_id] = message
                else:
                    extras.append(message)
        ordered = [by_id[tc["id"]] for tc in tool_calls if tc.get("id") in by_id]
        return ordered + extras

    async def execute_pending_tools(self) -> None:
        """Run the pending response's tool calls.

        Synchronous batches proceed directly to autosave. Batches containing a
        deferrable tool are buffered as a unit so their result block preserves
        provider call order while the session waits for outstanding work.
        """
        pending = self._pending
        if pending is None:
            self.state.fail()
            raise RuntimeError("Cannot execute tools before calling LLM")

        original_tool_calls = list(pending.response.tool_calls)
        preflight = getattr(self.tool_execution, "preflight_tool_batch", None)
        rejected_batch = getattr(
            self.tool_execution,
            "preflight_rejection_result",
            None,
        )
        if callable(preflight) and callable(rejected_batch):
            preflight_errors = preflight(original_tool_calls)
            if any(preflight_errors):
                rejected_batch(
                    original_tool_calls,
                    preflight_errors,
                ).apply_to(self.state)
                self._pending_tool_allowlist = None
                self._pending_tool_gate_label = None
                self.state.transition_to(SessionPhase.AUTOSAVING)
                return
        tool_calls, blocked_messages = self._apply_pending_tool_allowlist(
            original_tool_calls
        )
        _immediate, deferred = self._split_tool_calls(tool_calls)

        if not deferred:
            result = (
                await self.tool_execution.process(tool_calls)
                if tool_calls
                else ToolProcessingResult()
            )
            result.messages_to_append = self._ordered_tool_messages(
                original_tool_calls,
                result.messages_to_append,
                blocked_messages,
            )
            result.apply_to(self.state)
            self._record_submission(result)
            self._pending_tool_allowlist = None
            self._pending_tool_gate_label = None
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        table = self.state.pending_events
        order = {tc["id"]: i for i, tc in enumerate(original_tool_calls)}
        completed_messages = list(blocked_messages)
        observations = ToolProcessingResult()
        terminal_capture_accepted = False
        for tc in tool_calls:
            if terminal_capture_accepted:
                completed_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": TERMINAL_CAPTURE_SKIP_MESSAGE,
                    }
                )
                continue
            if tc.get("function", {}).get("name") in self.deferrable_tool_names:
                await self._execute_deferred_tools(table, order, [tc])
                continue

            proc = await self.tool_execution.process([tc])
            proc.apply_hashes_to(self.state)
            observations.reads_executed += proc.reads_executed
            observations.write_succeeded |= proc.write_succeeded
            observations.read_write_signals.extend(proc.read_write_signals)
            observations.evidence_signals.extend(proc.evidence_signals)
            observations.evidence_cards.extend(proc.evidence_cards)
            observations.loop_detections.extend(proc.loop_detections)
            observations.tool_step_attempted |= proc.tool_step_attempted
            completed_messages.extend(proc.messages_to_append)
            terminal_capture_accepted = proc.terminal_capture_accepted
            self._record_submission(proc)

        observations.apply_read_write_counter_to(self.state)
        observations.apply_evidence_counter_to(self.state)
        self._buffer_completed_rows(table, order, completed_messages)

        self._pending_tool_allowlist = None
        self._pending_tool_gate_label = None
        if table.is_complete():
            for message in table.ordered_results():
                self.state.append_message(message)
            table.clear()
            self.state.transition_to(SessionPhase.AUTOSAVING)
            return

        elapsed = pending.latency
        if pending.started_at is not None:
            elapsed = max(elapsed, time.monotonic() - pending.started_at)
        self.state.pending_step_latency = elapsed
        self._pending = None
        self.state.transition_to(SessionPhase.AWAITING_EVENTS)

    @staticmethod
    def _buffer_completed_rows(
        table: PendingEventTable,
        order: dict[str, int],
        messages: list[dict],
    ) -> None:
        for message in messages:
            tool_call_id = message["tool_call_id"]
            table.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.IMMEDIATE,
                    order=order[tool_call_id],
                    status=RowStatus.DONE,
                    result=message["content"],
                )
            )

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
        # Closed-loop steering: build a fresh per-turn block from the live
        # counters. When history ends on a USER turn (the start of a turn) the
        # block is folded into that message IN PLACE — no new message, so indices
        # and the timestamp sidecar are unchanged — and thus SAVED to the
        # transcript. On a continuation step the tail is a tool/assistant message
        # with no user turn to fold into, so the block rides in the shaped copy
        # only: the model still sees it, but it stays out of the persisted history.
        tool_names = {getattr(t, "name", None) for t in getattr(self.agent, "tools", []) or []}
        steering, tool_choice_override, steering_level = build_steering_block(
            used_tokens=self.state.used_tokens,
            max_budget_tokens=self.max_budget_tokens or 0,
            step_count=self.state.step_count + 1,
            max_steps=self.max_steps,
            reads=self.state.turn.reads_since_last_edit,
            has_write=bool(tool_names & _WRITE_TOOLS),
            has_structured_output=_STRUCTURED_OUTPUT_TOOL in tool_names,
            structured_override=_submit_tool_choice(_STRUCTURED_OUTPUT_TOOL),
            write_landed=self.state.turn.has_landed_write,
            budget_nudge_mode=resolve_budget_nudge_mode(),
            write_nudge_mode=resolve_write_nudge_mode(),
            prev_used_tokens=(
                self.state.used_tokens
                if self._steering_prev_used_tokens is None
                else self._steering_prev_used_tokens
            ),
        )
        # Spend at the turn just built, so the next turn can see a band crossing.
        self._steering_prev_used_tokens = self.state.used_tokens
        self._maybe_trace_steering(steering_level)
        persisted = steering is not None and bool(self.state.messages) and self.state.messages[-1].get("role") == "user"
        if persisted:
            self.state.messages[-1] = fold_steering(self.state.messages[-1], steering["content"])
        messages = self._shape_and_trace(self.state.messages)
        if steering is not None and not persisted:
            messages = [*messages, steering]
        if steering_level == "hard":
            hard_tools, gate_label = self._hard_steering_tools(tool_choice_override)
            self._pending_tool_allowlist = hard_tools
            self._pending_tool_gate_label = gate_label
            tools = self._tool_schemas_with_names(tools, hard_tools)
        else:
            self._pending_tool_allowlist = None
            self._pending_tool_gate_label = None

        try:
            return await self._complete(messages, tools, tool_choice_override)
        except Exception as exc:
            if not self._is_context_overflow(exc):
                raise

        # First overflow: force a maximal compaction pass and retry once.
        forced = forced_shape(self.shaper, self.state.messages) if self.shaper is not None else self.state.messages
        # No FRESH steering on the emergency-shrink retry: this path is fighting
        # for token space. Any budget folded into a trailing user turn already
        # rides along in history; no new block is added here.
        logger.warning(
            "context overflow on aid=%s: recompacting (%d -> %d messages) and retrying once",
            self.state.aid,
            len(messages),
            len(forced),
        )
        await self.event_publisher.emit(self.event_factory.error("context_overflow_recompacted"))
        try:
            return await self._complete(
                forced,
                tools,
                tool_choice_override,
            )
        except Exception as exc:
            if self._is_context_overflow(exc):
                raise _ContextOverflowStop() from exc
            raise

    async def _complete(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice_override: Any | None = None,
    ) -> CompletionResponse:
        # ``tool_choice`` is read defensively (getattr) so duck-typed agent stubs
        # without the field keep working; ``None`` (the default) keeps the
        # provider's "auto", so the surface is unchanged for ordinary agents.
        # When an agent requests "required" (the forced-write coder) but the
        # provider rejects it (some OpenAI-compatible endpoints don't support
        # it), fall back ONCE to "auto" — the explicit forced prompt and the
        # diff-gate still push the model to write.
        # ``tool_choice_override`` (the steering hard-rung) wins when set, so a
        # read-without-write escalation can force a tool call this turn.
        tool_choice = tool_choice_override or getattr(self.agent, "tool_choice", None)
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
            if not _is_tool_choice_rejection(exc):
                raise
            logger.warning(
                "tool_choice=%r rejected on aid=%s (%s); retrying once with 'auto'",
                tool_choice,
                self.state.aid,
                type(exc).__name__,
            )
            try:
                return await self._complete_with_choice(messages, tools, "auto")
            except Exception as fallback_exc:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "Retrying with tool_choice='auto' also failed: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    )
                raise exc from fallback_exc

    async def _complete_with_choice(
        self, messages: list[dict], tools: list[dict] | None, tool_choice: Any | None
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
        # ``top_p`` is read defensively and included ONLY when set, mirroring
        # ``tool_choice``: an agent without it (or with top_p=None) calls
        # ``complete`` with exactly the prior kwargs, so duck-typed LLM stubs that
        # never added the param keep working and the request is unchanged.
        top_p = getattr(self.agent, "top_p", None)
        if top_p is not None:
            extra["top_p"] = top_p
        configured_output_tokens = getattr(
            self.agent,
            "max_tokens_per_step",
            DEFAULT_MAX_TOKENS_PER_STEP,
        )
        remaining_budget = int(self.max_budget_tokens) - int(self.state.used_tokens)
        # Reserve what the request will actually carry. The outbound normalizer
        # strips ``reasoning_content`` on every streaming call
        # (openai_provider._build_request_kwargs passes
        # ``keep_reasoning_content=not stream``), so counting recorded reasoning
        # here reserved input the provider never billed and stopped sessions
        # that still held most of their budget.
        reserved_input_tokens = estimate_request_tokens(
            messages,
            tools,
            keep_reasoning_content=not getattr(self.agent, "llm_stream_chat", False),
        )
        output_budget = remaining_budget - reserved_input_tokens
        if output_budget < 1:
            raise _TokenBudgetStop(
                reserved_input_tokens=reserved_input_tokens,
                remaining_budget=remaining_budget,
            )
        # Precheck guarantees positive headroom before entering this call. Clamp
        # the provider's output ceiling to the live remainder after reserving an
        # estimate for request messages and registered tool schemas.
        max_output_tokens = min(
            max(1, int(configured_output_tokens)),
            output_budget,
        )
        if max_output_tokens != DEFAULT_MAX_TOKENS_PER_STEP:
            extra["max_output_tokens"] = max_output_tokens
        reasoning_effort = getattr(self.agent, "reasoning_effort", None)
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort
        if getattr(self.llm, "supports_response_session_identity", False):
            extra["response_session_id"] = self._response_session_id
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
        await self._start_llm_step()
        if self._per_call_timeout is None:
            return await self.llm.complete(**kwargs)
        try:
            protected_call = self.state.wind_down_done
            return await abandon_on_timeout(
                self.llm.complete(**kwargs),
                self._per_call_timeout,
                task_tracker=self._track_provider_task,
                late_task_tracker=self._mark_provider_task_draining,
                late_result_handler=lambda task: self._record_late_provider_result(
                    task,
                    protected_call=protected_call,
                ),
            )
        except CallerTimeoutError as exc:
            raise GenerationTimeoutError(
                f"LLM generation exceeded the {self._per_call_timeout}s per-call timeout"
            ) from exc

    async def _start_llm_step(self) -> None:
        """Record one step immediately before the first provider attempt."""
        if self._llm_step_started:
            return
        self.state.advance_step()
        await self.event_publisher.emit(
            self.event_factory.step_start(self.state.step_count)
        )
        self._llm_step_started = True

    async def _stop_on_context_overflow(self) -> None:
        """Graceful terminal stop when a prompt overflows the model window even
        after forced compaction + one retry. Mirrors the budget-exhaustion
        degradation: a visible system message, an error event, and a validated
        transition to STOPPED (reason names the overflow). A child stopped this
        way delivers a controlled result to its parent (DONE row) rather than
        crashing the parent's turn.
        """
        reason = "context overflow: prompt exceeds the model context window even after compaction"
        self.state.append_message({"role": "system", "content": f"[{reason.capitalize()}. Session stopped.]"})
        await self.event_publisher.emit(self.event_factory.error(reason))
        self.clear_pending_step()
        self.state.transition_to(SessionPhase.STOPPED, reason=reason)

    def _shape_and_trace(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Shape the model's view, recording which compaction rung really fired.

        The shapers reshape a COPY (the transcript keeps the full history), so
        nothing on disk would otherwise say which of the five rungs ran on a
        given turn. This emits one ``context_shaping`` record per fired rung —
        and exactly one ``rung="none"`` record when the turn passed through
        untouched, so a reader can tell "nothing fired" from "nothing was
        recorded". The rung labels are the shapers' own frozen names, which is
        what lets a run be compared turn-by-turn against its assigned policy.

        The pipeline stays a pure transform: it only reports, and the sink
        lives here, where ``tracer``/``aid``/``step_count`` are already at hand.
        """
        if self.tracer is None:
            return self.shaper.shape(messages) if self.shaper is not None else messages
        # A ShaperPipeline names its own rungs; wrap anything else (a single
        # shaper, or nothing wired at all) so every turn still reports.
        pipeline = (
            self.shaper
            if isinstance(self.shaper, ShaperPipeline)
            else ShaperPipeline(() if self.shaper is None else (self.shaper,))
        )
        shaped, reports = pipeline.shape_with_report(messages)
        for report in reports:
            self.tracer.log_step(
                step_type="context_shaping",
                payload={
                    "seq": self.state.step_count,
                    "aid": self.state.aid,
                    **report,
                },
            )
        return shaped

    def _maybe_trace_steering(self, level: str | None) -> None:
        """Emit a ``steering_nudge`` trace step on an UPWARD level crossing.

        ``reads_since_last_edit`` can jump past 8/16 in one batch, so the high-
        water mark (``_last_steering_level``), not equality, decides whether this
        is a new escalation. A genuine write reset re-arms so a later re-escalation
        traces again. The mark advances even when no tracer is wired, so the next
        escalation still computes correctly.
        """
        rank = {None: 0, "soft": 1, "hard": 2}
        if level is None:
            # ``level is None`` means no active write nudge this turn. Re-arm the
            # high-water mark only when a write reset actually dropped reads below
            # the soft rung; if reads is still high (e.g. a read-only session that
            # never escalates), the escalation has NOT de-escalated, so leave the
            # mark intact (re-arming would let a later still-high turn re-fire a
            # duplicate steering_nudge).
            if self.state.turn.reads_since_last_edit < READS_NUDGE_SOFT:
                self._last_steering_level = None
            return
        if rank[level] > rank[self._last_steering_level] and self.tracer is not None:
            self.tracer.log_step(
                step_type="steering_nudge",
                payload={
                    "aid": self.state.aid,
                    "agent": getattr(self.agent, "role", None)
                    or getattr(self.agent, "label", None)
                    or self.agent.model,
                    "reads_since_last_edit": self.state.turn.reads_since_last_edit,
                    "level": level,
                    "tool_choice_override": level == "hard",
                    "step": self.state.step_count,
                },
            )
        self._last_steering_level = level  # update high-water mark even with no tracer

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
            usage = response.usage
            input_tokens = getattr(usage, "input_tokens", 0)
            total_tokens = getattr(usage, "total_tokens", input_tokens)
            payload = {
                # Agent attribution: the same ``aid`` steering_nudge/commit_brake
                # stamp, so every record in a multi-agent trajectory file joins
                # on one field.
                "aid": self.state.aid,
                "model": self.agent.model,
                "finish_reason": response.finish_reason,
                "content": response.content,
                "tool_calls": tool_calls_log,
            }
            if usage is not None:
                output_tokens = getattr(usage, "output_tokens", max(total_tokens - input_tokens, 0))
                cache_read_tokens = getattr(usage, "cache_read_tokens", 0)
                cache_creation_tokens = getattr(usage, "cache_creation_tokens", 0)
                payload["usage"] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "uncached_input_tokens": (
                        max(input_tokens - cache_read_tokens - cache_creation_tokens, 0)
                        if cache_read_tokens is not None and cache_creation_tokens is not None
                        else None
                    ),
                    "estimated": getattr(usage, "estimated", False),
                }
                reasoning_tokens = getattr(usage, "reasoning_tokens", None)
                if reasoning_tokens is not None:
                    payload["usage"]["reasoning_tokens"] = reasoning_tokens
                raw_usage = getattr(usage, "raw_usage", None)
                if raw_usage:
                    payload["usage"]["raw_usage"] = raw_usage
            # Record provider chain-of-thought to the trajectory when present
            # (omitted otherwise, so non-thinking traces keep their prior shape).
            reasoning = getattr(response, "reasoning", None)
            if reasoning:
                payload["reasoning"] = reasoning
            elif payload.get("usage", {}).get("reasoning_tokens"):
                # The provider billed for reasoning and returned none of it. The
                # step is then unreconstructible -- a turn that emitted tool calls
                # and no text leaves nothing at all saying why it did that -- and
                # the absence is invisible in a trajectory that simply has no
                # reasoning field. Say it, so a run can be audited for this
                # rather than discovered to have lost it afterwards.
                payload["reasoning_withheld"] = True
            payload["thinking"] = bool(getattr(self.agent, "thinking", False))
            wire_protocol = getattr(self.agent, "wire_protocol", "chat_completions")
            if wire_protocol != "chat_completions":
                payload["wire_protocol"] = wire_protocol
            reasoning_effort = getattr(self.agent, "reasoning_effort", None)
            if reasoning_effort is not None:
                payload["reasoning_effort"] = reasoning_effort
            payload["reasoning_effort_policy"] = getattr(
                self.agent,
                "reasoning_effort_policy",
                "configured",
            )
            provider_model = getattr(response, "provider_model", None)
            if provider_model is not None:
                payload["provider_model"] = provider_model
            self.tracer.log_step(
                step_type="llm_call",
                payload=payload,
                tokens=total_tokens,
                latency=latency,
            )

    def append_assistant_message(self, response: CompletionResponse) -> None:
        has_content = (
            isinstance(response.content, str)
            and bool(response.content.strip())
        )
        # A provider-limit response may contain an incomplete tool call. Never
        # persist that structure: a later turn would send an orphaned call back
        # to the provider, and the run loop must not execute partial arguments.
        # Preserve only user-visible partial text; the full raw response remains
        # available in the trace for diagnosis.
        if response.finish_reason in {"length", "max_tokens"}:
            if has_content:
                self.state.append_message({"role": "assistant", "content": response.content})
            return
        # An empty-stop turn (no content, no tool calls) would append a bare
        # ``{"role": "assistant"}`` message that some providers reject on the
        # next request. Skip it — handle_pending_response decides retry-vs-DONE.
        if not has_content and not response.tool_calls:
            return
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        reasoning = getattr(response, "reasoning", None)
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        provider_state = getattr(response, "provider_state", None)
        if provider_state:
            assistant_msg["provider_state"] = provider_state
        if has_content:
            assistant_msg["content"] = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = response.tool_calls
        provider_items = getattr(response, "provider_items", None)
        if provider_items:
            assistant_msg["response_items"] = provider_items
        self.state.append_message(assistant_msg)

    async def finish_step(self, latency: float) -> None:
        # AutoSaveSubscriber listens for step_end on the bus.
        await self.event_publisher.emit(self.event_factory.step_end(self.state.step_count, latency))
