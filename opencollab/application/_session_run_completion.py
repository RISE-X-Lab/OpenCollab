"""Provider completion and post-response helpers for session execution."""

from __future__ import annotations

import logging
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
from opencollab.application.shaping import forced_shape
from opencollab.application.steering import (
    READS_NUDGE_SOFT,
    build_steering_block,
    fold_steering,
)
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP
from opencollab.domain.pending import PendingEventTable, PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import request_tokens_upper_bound

logger = logging.getLogger(__name__)


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

    def _write_only_tool_schemas(self, tools: list[dict] | None) -> list[dict] | None:
        return self._tool_schemas_with_names(tools, _WRITE_TOOLS)

    def _tool_schemas_with_names(self, tools: list[dict] | None, names: frozenset[str]) -> list[dict] | None:
        if not tools:
            return tools
        filtered = [spec for spec in tools if spec.get("function", {}).get("name") in names]
        return filtered or tools

    def _hard_steering_tools(self) -> tuple[frozenset[str], str]:
        tool_names = {getattr(t, "name", None) for t in getattr(self.agent, "tools", []) or []}
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
        )
        self._maybe_trace_steering(steering_level)
        persisted = steering is not None and bool(self.state.messages) and self.state.messages[-1].get("role") == "user"
        if persisted:
            self.state.messages[-1] = fold_steering(self.state.messages[-1], steering["content"])
        messages = self.shaper.shape(self.state.messages) if self.shaper is not None else self.state.messages
        if steering is not None and not persisted:
            messages = [*messages, steering]
        if steering_level == "hard":
            hard_tools, gate_label = self._hard_steering_tools()
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
            return await self._complete(forced, tools)
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
        reserved_input_tokens = request_tokens_upper_bound(messages, tools)
        output_budget = remaining_budget - reserved_input_tokens
        if output_budget < 1:
            raise _TokenBudgetStop(
                reserved_input_tokens=reserved_input_tokens,
                remaining_budget=remaining_budget,
            )
        # Precheck guarantees positive headroom before entering this call. Clamp
        # the provider's output ceiling to the live remainder after reserving an
        # upper bound for request messages and registered tool schemas.
        max_output_tokens = min(
            max(1, int(configured_output_tokens)),
            output_budget,
        )
        if max_output_tokens != DEFAULT_MAX_TOKENS_PER_STEP:
            extra["max_output_tokens"] = max_output_tokens
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
            return await abandon_on_timeout(
                self.llm.complete(**kwargs),
                self._per_call_timeout,
                task_tracker=self._track_provider_task,
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
                    "uncached_input_tokens": max(
                        input_tokens - cache_read_tokens - cache_creation_tokens,
                        0,
                    ),
                    "estimated": getattr(usage, "estimated", False),
                }
                raw_usage = getattr(usage, "raw_usage", None)
                if raw_usage:
                    payload["usage"]["raw_usage"] = raw_usage
            # Record provider chain-of-thought to the trajectory when present
            # (omitted otherwise, so non-thinking traces keep their prior shape).
            reasoning = getattr(response, "reasoning", None)
            if reasoning:
                payload["reasoning"] = reasoning
            payload["thinking"] = bool(getattr(self.agent, "thinking", False))
            self.tracer.log_step(
                step_type="llm_call",
                payload=payload,
                tokens=total_tokens,
                latency=latency,
            )

    def append_assistant_message(self, response: CompletionResponse) -> None:
        # A provider-limit response may contain an incomplete tool call. Never
        # persist that structure: a later turn would send an orphaned call back
        # to the provider, and the run loop must not execute partial arguments.
        # Preserve only user-visible partial text; the full raw response remains
        # available in the trace for diagnosis.
        if response.finish_reason in {"length", "max_tokens"}:
            if response.content:
                self.state.append_message({"role": "assistant", "content": response.content})
            return
        # An empty-stop turn (no content, no tool calls) would append a bare
        # ``{"role": "assistant"}`` message that some providers reject on the
        # next request. Skip it — handle_pending_response decides retry-vs-DONE.
        if not response.content and not response.tool_calls:
            return
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        reasoning = getattr(response, "reasoning", None)
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        provider_state = getattr(response, "provider_state", None)
        if provider_state:
            assistant_msg["provider_state"] = provider_state
        if response.content:
            assistant_msg["content"] = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = response.tool_calls
        self.state.append_message(assistant_msg)

    async def finish_step(self, latency: float) -> None:
        # AutoSaveSubscriber listens for step_end on the bus.
        await self.event_publisher.emit(self.event_factory.step_end(self.state.step_count, latency))
