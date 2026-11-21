"""Provider completion and post-response helpers for session execution."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable

from opencollab.application._anthropic_history import (
    _anthropic_history_input,
    _capture_anthropic_snips,
    _edited_anthropic_history,
    _recover_anthropic_history,
    _remember_anthropic_history,
    _strip_anthropic_thinking,
)
from opencollab.application._session_run_shared import (
    _STRUCTURED_OUTPUT_TOOL,
    _WRITE_TOOLS,
    GenerationTimeoutError,
    _ContextOverflowStop,
    _request_tool_names,
    _submit_tool_choice,
    _TeamBudgetStop,
    _TokenBudgetStop,
)
from opencollab.application._session_run_trace import _SessionRunTraceMixin
from opencollab.application._session_run_usage import (
    _complete_with_error_usage,
    _CompletionUsageAccounting,
    _normalize_completion_usage,
)
from opencollab.application._tool_loop_execution import _apply_completed_prefix_progress
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.ports import (
    _NATIVE_COMPLETION_CAPABILITY,
    _NATIVE_REQUEST_ADMISSION,
    CompletionResponse,
    ContextTokenEstimatorPort,
    RequestOutputRequirementPort,
    RequestTokenEstimatorPort,
    ThinkingPrefixPort,
    _native_completion_controls,
    _NativeCompletionControls,
)
from opencollab.application.shaping import ShaperPipeline
from opencollab.application.shaping.pipeline import context_token_estimator
from opencollab.application.steering import (
    build_steering_block,
    fold_steering,
    resolve_budget_nudge_mode,
    resolve_write_nudge_mode,
)
from opencollab.application.tool_execution import TERMINAL_CAPTURE_SKIP_MESSAGE
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP
from opencollab.domain.pending import PendingEventTable, PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_message_tokens, estimate_request_tokens
from opencollab.domain.tools import ToolProcessingResult, tool_name_collision_key

logger = logging.getLogger(__name__)

async def _with_native_completion_controls(
    operation: Awaitable[CompletionResponse],
    scope: _NativeCompletionControls | None,
) -> CompletionResponse:
    token = _native_completion_controls.set(scope)
    try:
        return await operation
    finally:
        if scope is not None:
            scope.callbacks.clear()
        _native_completion_controls.reset(token)

def _completion_keyword_parameters(
    complete: Callable[..., Awaitable[Any]],
) -> tuple[set[str] | None, bool, set[str], Callable[..., Any]]:
    keywords: set[str] = set()
    outer_accepts_kwargs = False
    outer_keywords: set[str] = set()
    target = complete

    def has_fixed_keywords(target: Callable[..., Any]) -> bool:
        nonlocal outer_accepts_kwargs
        parameters = inspect.signature(target, follow_wrapped=False).parameters.values()
        names = {
            parameter.name for parameter in parameters
            if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        keywords.update(names)
        accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        if target is complete:
            outer_accepts_kwargs = accepts_kwargs
            outer_keywords.update(names)
        return not accepts_kwargs

    # Follow variadic decorators through to the first fixed signature.
    try:
        target = inspect.unwrap(complete, stop=has_fixed_keywords)
        if not hasattr(target, "__wrapped__") and not has_fixed_keywords(target):
            return None, outer_accepts_kwargs, outer_keywords, target
    except (TypeError, ValueError):
        pass
    return keywords, outer_accepts_kwargs, outer_keywords, target

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
            rf'\b{choice}\b(?:\s+(?:parameter|field|value)|\s*:\s*type "tool" and "any" are)?'
            rf"\s+(?:(?:is|was)\s+)?(?:{rejection})\b",
            rf"\b(?:does\s+not\s+support|doesn't\s+support|rejects?|rejected)"
            rf"\s+(?:the\s+)?{choice}\b",
        )
    )

class _SessionRunCompletionMixin(_SessionRunTraceMixin):
    """Implementation details composed into ``SessionRunUseCase``."""

    def _requires_thinking_prefix(self) -> bool:
        return isinstance(self.llm, ThinkingPrefixPort) and self.llm.requires_thinking_prefix

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
            try:
                ref, error = await self.tool_execution.execute_deferred(tool_call)
            except asyncio.CancelledError:
                self._settle_cancelled_deferred_row(table, tool_call_id)
                raise
            self._record_deferred_result(table, tool_call_id, ref, error)

    def _record_deferred_result(
        self,
        table: PendingEventTable,
        tool_call_id: str,
        ref: int | None,
        error: str | None,
    ) -> None:
        row = table.rows[tool_call_id]
        if ref is not None:
            table.rows[tool_call_id] = replace(row, ref=ref)
            return
        if row.status is not RowStatus.PENDING:
            return
        message = error or "Deferred tool failed without an error message."
        table.fill(tool_call_id, status=RowStatus.FAILED, result=message, error=message)

    def _settle_cancelled_deferred_row(self, table: PendingEventTable, tool_call_id: str) -> None:
        """Keep a produced child, and fail a row that never acquired one."""
        row = table.rows.get(tool_call_id)
        if row is None:
            return
        pending = getattr(self.tool_execution, "_pending_deferred_result", None)
        if isinstance(pending, tuple) and len(pending) == 2 and pending[0] is not None:
            table.rows[tool_call_id] = replace(row, ref=pending[0])
            return
        if row.status is not RowStatus.PENDING or row.ref is not None:
            return
        if isinstance(pending, tuple) and len(pending) == 2 and pending[1] is not None:
            self._record_deferred_result(table, tool_call_id, None, str(pending[1]))
            return
        message = (
            "Tool execution interrupted by session cancellation. "
            "The execution outcome is unknown."
        )
        table.fill(tool_call_id, status=RowStatus.FAILED, result=message, error=message)

    def _is_deferred_tool_name(self, name: object) -> bool:
        try:
            key = tool_name_collision_key(name)
        except ValueError:
            return False
        return any(tool_name_collision_key(allowed) == key for allowed in self.deferrable_tool_names)

    def _split_tool_calls(self, tool_calls: list[dict]) -> tuple[list[dict], list[dict]]:
        """Partition a batch into (immediate, deferred) by deferrable name."""
        immediate: list[dict] = []
        deferred: list[dict] = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name")
            if self._is_deferred_tool_name(name):
                deferred.append(tc)
            else:
                immediate.append(tc)
        return immediate, deferred

    def _record_submission(self, result: Any) -> None:
        """Remember that this step called ``submit``, and with what."""
        if getattr(result, "turn_submitted", False):
            self.state.submitted_summary = getattr(result, "submitted_summary", None) or ""

    async def autosave_pending_step(self) -> None:
        """Emit the autosave-triggering ``step_end``, then choose PRECHECK or DONE.

        A submitted step first appends the submitted summary as the turn's answer
        and follows the same autosave path before returning DONE.
        """
        pending = self._pending
        latency = (
            pending.latency
            if pending is not None
            else (self.state.pending_step_latency or 0.0)
        )
        submitted = self.state.submitted_summary
        if submitted is not None:
            # The assistant response also survives deferred work and reloads,
            # where the in-memory pending response has already been cleared.
            response_content = next(
                (message.get("content") for message in reversed(self.state.messages) if message["role"] == "assistant"),
                None,
            )
            if isinstance(response_content, str):
                response_content = response_content.strip()
            self.state.append_message({"role": "assistant", "content": submitted})
            if submitted and submitted != response_content:
                await self.event_publisher.emit(self.event_factory.text_delta(submitted))
        await self.finish_step(latency)
        self.clear_pending_step()
        self._ensure_tool_environment_active()
        if submitted is not None:
            self.state.submitted_summary = None
            self.state.transition_to(SessionPhase.DONE, reason="submitted")
            return
        self.state.transition_to(SessionPhase.PRECHECK)

    def clear_pending_step(self) -> None:
        self._pending = None
        self.state.pending_step_latency = None

    def build_tool_schemas(self) -> list[dict] | None:
        schemas = self.agent.tool_schemas()
        environment = getattr(self.tool_execution, "environment", None)
        if environment is not None and not getattr(environment, "process_isolated", False):
            blocked = {tool.name for tool in self.agent.tools
                       if getattr(tool, "require_process_isolation", False) is True}
            schemas = [
                {**schema, "function": {**schema["function"], "description": (
                    "Unavailable in the current execution environment: this tool requires an OS process sandbox. "
                    "Use another available tool or report that an isolated execution environment is required. "
                    + schema["function"].get("description", "")
                )}} if schema["function"]["name"] in blocked else schema
                for schema in schemas
            ]
        return schemas or None

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
        if tool_calls:
            self._required_tool_retried = False
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
        self._buffer_completed_rows(table, order, blocked_messages)
        observations = ToolProcessingResult(
            model_step=self.state.step_count if self.state.step_count > 0 else None,
        )
        terminal_capture_accepted = False
        for tc in tool_calls:
            if terminal_capture_accepted:
                self._buffer_completed_rows(table, order, [
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": TERMINAL_CAPTURE_SKIP_MESSAGE,
                    }
                ])
                continue
            if self._is_deferred_tool_name(tc.get("function", {}).get("name")):
                await self._execute_deferred_tools(table, order, [tc])
                continue

            proc = await self.tool_execution.process([tc])
            proc.apply_hashes_to(self.state)
            proc.apply_read_write_counter_to(self.state)
            self._buffer_completed_rows(table, order, proc.messages_to_append)
            observations.reads_executed += proc.reads_executed
            observations.write_succeeded |= proc.write_succeeded
            observations.read_write_signals.extend(proc.read_write_signals)
            observations.current_progress |= proc.current_progress
            observations.effect_unknown |= proc.effect_unknown
            observations.evidence_neutral.extend(proc.evidence_neutral)
            observations.evidence_signals.extend(proc.evidence_signals)
            observations.evidence_cards.extend(proc.evidence_cards)
            observations.loop_detections.extend(proc.loop_detections)
            observations.tool_step_attempted |= proc.tool_step_attempted
            _apply_completed_prefix_progress(self.state, observations)
            terminal_capture_accepted = proc.terminal_capture_accepted
            self._record_submission(proc)

        # Immediate results have folded their ordered read/write prefix already.
        observations.read_write_applied_count = len(observations.read_write_signals)
        observations.apply_evidence_counter_to(self.state)

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
            previous = table.rows.get(tool_call_id)
            if previous is not None and previous.status is RowStatus.DONE and previous.result == message["content"]:
                continue
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
        # Models with prefix-bound thinking retain every sent steering message
        # in the transcript. Other continuations keep the request-only nudge.
        tool_names = {getattr(t, "name", None) for t in getattr(self.agent, "tools", []) or []}
        steering, tool_choice_override, steering_level = build_steering_block(
            used_tokens=self.state.used_tokens,
            max_budget_tokens=self.max_budget_tokens,
            step_count=self.state.step_count + 1,
            max_steps=self.max_steps,
            reads=0 if self.state.turn.write_effect_unknown else self.state.turn.reads_since_last_edit,
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
        elif steering is not None and self._requires_thinking_prefix():
            self.state.append_message(steering)
        preserve_history = self._requires_thinking_prefix()
        history = _anthropic_history_input(self.state)
        with _capture_anthropic_snips(self.state if preserve_history else None) as adopted_snips:
            messages = await self._shape_and_trace(history)
            snipped = adopted_snips(history, messages)
        if preserve_history:
            messages = _edited_anthropic_history(history, messages)
        elif steering is not None and not persisted:
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
            return await self._complete(messages, tools, tool_choice_override, snipped_exchanges=snipped)
        except Exception as exc:
            if not self._is_context_overflow(exc):
                raise

        # First overflow: force a maximal compaction pass and retry once.
        with (
            context_token_estimator(self._context_pressure),
            _capture_anthropic_snips(self.state if preserve_history else None) as adopted_snips,
        ):
            history = _anthropic_history_input(self.state)
            forced = (
                await ShaperPipeline((self.shaper,)).ashape(history, force=True)
                if self.shaper is not None else history
            )
            emergency_snipped = adopted_snips(history, forced)
        if preserve_history:
            forced = _edited_anthropic_history(history, forced)
        # No FRESH steering on the emergency-shrink retry: this path is fighting
        # for token space. Any budget folded into a trailing user turn already
        # rides along in history; no new block is added here.
        logger.warning(
            "context overflow on aid=%s: recompacting (%d -> %d messages) and retrying once",
            self.state.aid,
            len(messages),
            len(forced),
        )
        self._emit_run_event("context_shaping", emergency=True, before_messages=len(messages),
                             after_messages=len(forced))
        await self.event_publisher.emit(self.event_factory.error("context_overflow_recompacted"))
        try:
            return await self._complete(
                forced,
                tools,
                tool_choice_override,
                snipped_exchanges=emergency_snipped,
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
        *,
        snipped_exchanges: set[int] | None = None,
    ) -> CompletionResponse:
        # Duck-typed agents may omit tool_choice. A steering override takes precedence,
        # and unsupported forced choices get one retry with "auto".
        tool_choice = tool_choice_override or getattr(self.agent, "tool_choice", None)
        try:
            return await self._complete_with_choice(messages, tools, tool_choice, snipped_exchanges)
        except Exception as exc:
            forced_choice = tool_choice == "required" or (
                isinstance(tool_choice, dict)
                and tool_choice.get("type") in {"any", "tool", "function"}
            )
            if not forced_choice or self._is_context_overflow(exc):
                raise
            # Retry only an explicit tool_choice rejection. Transient, auth, and server
            # failures also fail with "auto". Duck typing keeps provider imports out of this layer.
            if not _is_tool_choice_rejection(exc):
                raise
            logger.warning(
                "tool_choice=%r rejected on aid=%s (%s); retrying once with 'auto'",
                tool_choice,
                self.state.aid,
                type(exc).__name__,
            )
            try:
                return await self._complete_with_choice(messages, tools, "auto", snipped_exchanges)
            except Exception as fallback_exc:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "Retrying with tool_choice='auto' also failed: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    )
                raise exc from fallback_exc

    async def _complete_with_choice(
        self, messages: list[dict], tools: list[dict] | None, tool_choice: Any | None,
        snipped_exchanges: set[int] | None = None,
    ) -> CompletionResponse:
        # Capture the application-stage request after steering and fallback, before
        # provider conversion, for record_llm_trace. These fields only serve tracing.
        self._last_request_tool_names = _request_tool_names(tools)
        self._last_request_tool_choice = tool_choice
        # Omit unset optional kwargs for compatibility with duck-typed LLM clients.
        extra: dict[str, Any] = {}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        top_p = getattr(self.agent, "top_p", None)
        if top_p is not None:
            extra["top_p"] = top_p
        thinking = getattr(self.agent, "thinking", False)
        params = getattr(self.agent, "thinking_params", None)
        explicit_params = getattr(self.agent, "thinking_params_explicit", False)
        if getattr(self.agent, "wire_protocol", "chat_completions") == "responses" and not explicit_params:
            params = None
        if thinking or (params and explicit_params):
            extra.update(thinking=thinking, thinking_params=params)
        else:
            params = None
        await self._prepare_request_budget(messages, tools, thinking=thinking, thinking_params=params)
        if self._requires_thinking_prefix():
            messages = _remember_anthropic_history(self.state, messages, tools, snipped_exchanges)
        max_output_tokens = self._request_output_limit(
            messages, tools, thinking=thinking, thinking_params=params,
        )
        reasoning_effort = getattr(self.agent, "reasoning_effort", None)
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort
        if getattr(self.llm, "supports_response_session_identity", False):
            extra["response_session_id"] = self._response_session_id
        try:
            return await self._invoke_llm(
                messages=messages,
                tools=tools,
                temperature=self.agent.temperature,
                max_output_tokens=max_output_tokens,
                **extra,
            )
        except Exception as exc:
            recovery = (
                _recover_anthropic_history(self.state, messages, tools, exc)
                if self._requires_thinking_prefix() else None
            )
            if recovery is None:
                raise
            rejected_by = type(exc).__name__
        messages, removed = recovery
        await self._prepare_request_budget(messages, tools, thinking=thinking, thinking_params=params)
        messages = _remember_anthropic_history(self.state, messages, tools)
        max_output_tokens = self._request_output_limit(
            messages, tools, thinking=thinking, thinking_params=params,
        )
        payload = {
            "reason": "prefix_binding_mismatch", "removed_blocks": removed, "retry": 1,
            "error_type": rejected_by, "status_code": 400,
        }
        logger.warning(
            "native thinking prefix rejected on aid=%s; retiring %d thinking blocks and retrying once",
            self.state.aid, removed,
        )
        self._emit_run_event("thinking_prefix_recovery", **payload)
        if self.tracer is not None:
            self.tracer.log_step(
                step_type="thinking_prefix_recovery",
                payload={"aid": self.state.aid, "session_step": self.state.step_count, **payload},
            )
        return await self._invoke_llm(
            messages=messages,
            tools=tools,
            temperature=self.agent.temperature,
            max_output_tokens=max_output_tokens,
            **extra,
        )

    def _request_budget_requirements(
        self, messages: list[dict], tools: list[dict] | None, *, thinking: bool, estimate_input: bool = True,
        thinking_params: dict[str, Any] | None = None,
    ) -> tuple[int, int, int]:
        configured = max(1, int(getattr(self.agent, "max_tokens_per_step", DEFAULT_MAX_TOKENS_PER_STEP)))
        if thinking_params is None and thinking:
            thinking_params = getattr(self.agent, "thinking_params", None)
        minimum = 1
        if isinstance(self.llm, RequestOutputRequirementPort):
            minimum = self.llm.minimum_output_tokens(
                max_output_tokens=configured, thinking=thinking,
                thinking_params=thinking_params,
            )
        if not estimate_input:
            reserved = 0
        elif isinstance(self.llm, RequestTokenEstimatorPort):
            reserved = self.llm.estimate_request_tokens(
                messages, tools, thinking=thinking,
                thinking_params=thinking_params,
            )
        else:
            reserved = estimate_request_tokens(messages, tools)
        return configured, minimum, reserved

    def _estimate_context_message(self, message: dict[str, Any]) -> int:
        """Estimate one outbound message, excluding once-per-request framing."""
        if isinstance(self.llm, ContextTokenEstimatorPort):
            kwargs = {
                "thinking": getattr(self.agent, "thinking", False),
                "thinking_params": getattr(self.agent, "thinking_params", None),
            }
            return self.llm.estimate_context_tokens([message], **kwargs)
        return estimate_request_message_tokens([message])

    async def _prepare_request_budget(
        self, messages: list[dict], tools: list[dict] | None, *, thinking: bool,
        thinking_params: dict[str, Any] | None = None,
    ) -> None:
        if self._run_control is None:
            return
        _configured, minimum, reserved = self._request_budget_requirements(
            messages, tools, thinking=thinking, thinking_params=thinking_params,
        )
        await self._decide_run_budget(
            reason="request", reserved_input_tokens=reserved, minimum_output_tokens=minimum,
        )
        self._raise_if_run_cancelled()
        if self._run_final_prompt is not None:
            prompt = {"role": "system", "content": self._run_final_prompt}
            if prompt not in messages:
                messages.append(prompt)

    def _request_output_limit(
        self, messages: list[dict], tools: list[dict] | None, *, thinking: bool,
        thinking_params: dict[str, Any] | None = None,
    ) -> int:
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            raise _TeamBudgetStop
        max_output_tokens, minimum_output_tokens, reserved_input_tokens = self._request_budget_requirements(
            messages, tools, thinking=thinking, thinking_params=thinking_params,
            estimate_input=self.max_budget_tokens is not None or (
                self._run_control is not None and self.hard_budget_tokens is not None
            ),
        )
        remaining_budget = self.remaining_budget_tokens
        if remaining_budget is not None:
            output_budget = remaining_budget - reserved_input_tokens
            if output_budget < minimum_output_tokens:
                raise _TokenBudgetStop(
                    reserved_input_tokens=reserved_input_tokens, remaining_budget=remaining_budget,
                    minimum_output_tokens=minimum_output_tokens,
                )
            max_output_tokens = min(max_output_tokens, output_budget)
        return max_output_tokens

    @property
    def remaining_budget_tokens(self) -> int | None:
        ceilings = [self.max_budget_tokens]
        if self._run_control is not None:
            ceilings.append(self.hard_budget_tokens)
        finite = [int(ceiling) for ceiling in ceilings if ceiling is not None]
        if not finite:
            return None
        return min(finite) - int(self.state.used_tokens) - sum(reserve() for reserve in self.budget_reservations)

    def _add_completion_controls(
        self,
        complete: Callable[..., Awaitable[CompletionResponse]],
        kwargs: dict[str, Any],
        *,
        max_output_tokens: int,
        usage_accounting: _CompletionUsageAccounting,
        revalidate: Callable[[], int],
    ) -> tuple[asyncio.Event | None, _NativeCompletionControls | None]:
        keywords, outer_accepts_kwargs, outer_keywords, wrapped = _completion_keyword_parameters(complete)
        owner = getattr(complete, "__self__", None)
        wraps_base_method = owner is not None and any(
            vars(base).get("complete") is wrapped for base in type(owner).__mro__[1:]
        )
        # A metadata-only signature leaves admission on the outer router's kwargs.
        accepts_admission = outer_accepts_kwargs and not wraps_base_method
        scoped: dict[str, Callable[..., Any]] = {}

        def accepts(name: str) -> bool:
            return keywords is None or name in keywords

        def add_control(
            name: str,
            capability: str,
            callback: Callable[..., Any],
            *,
            native_support: object = _NATIVE_COMPLETION_CAPABILITY,
            allow_scope: bool = True,
        ) -> bool:
            supported = getattr(self.llm, capability, False)
            if not supported:
                return False
            if name in outer_keywords or (name == "on_admitted" and (accepts(name) or accepts_admission)):
                kwargs[name] = callback
                return True
            # A native marker also appears on subclasses whose wrappers forward
            # only legacy parameters. Supply their controls at native dispatch.
            inherited = inspect.getattr_static(owner, capability, None)
            declared = inspect.getattr_static(self.llm, capability, inherited)
            if declared is native_support:
                if allow_scope and inherited is native_support:
                    scoped[name] = callback
                    return True
                return False
            if declared is None and native_support is _NATIVE_COMPLETION_CAPABILITY:
                if keywords is None or name not in keywords:
                    return False
            # Explicit middleware opt-ins can consume controls before forwarding.
            if accepts(name) or (outer_accepts_kwargs and (supported is True or name == "on_admitted")):
                kwargs[name] = callback
                return True
            return False

        def revalidate_budget() -> int:
            limit = revalidate()
            usage_accounting.mark_request_revalidated()
            return limit

        if max_output_tokens != DEFAULT_MAX_TOKENS_PER_STEP or accepts("max_output_tokens"):
            kwargs["max_output_tokens"] = max_output_tokens
        add_control("on_attempt_usage", "supports_attempt_usage", usage_accounting.record_attempt)
        add_control("on_usage", "supports_usage_settlement", usage_accounting.settle_provider)
        add_control("before_request", "supports_request_revalidation", revalidate_budget)
        admitted = asyncio.Event()
        has_admission = add_control(
            "on_admitted", "supports_request_admission", admitted.set,
            native_support=_NATIVE_REQUEST_ADMISSION,
            allow_scope=self._per_call_timeout is not None and outer_accepts_kwargs,
        )
        scope = _NativeCompletionControls(kwargs.get("on_admitted"), scoped) if scoped else None
        return admitted if has_admission else None, scope

    def _revalidate_request(
        self, messages: list[dict], tools: list[dict] | None, *, thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
    ) -> int:
        self._raise_if_run_cancelled()
        return self._request_output_limit(
            messages, tools, thinking=thinking, thinking_params=thinking_params,
        )

    async def _invoke_summary(
        self, complete: Callable[..., Awaitable[CompletionResponse]], messages: list[dict],
        *, _summary_output_limit: Callable[[list[dict]], int] | None = None, **kwargs: Any,
    ) -> CompletionResponse:
        """Reserve, own and charge a summary call exactly once."""
        if self._requires_thinking_prefix():
            messages = _strip_anthropic_thinking(messages)
        thinking = kwargs.get("thinking", False)
        thinking_params = kwargs.get("thinking_params")
        await self._prepare_request_budget(messages, None, thinking=thinking, thinking_params=thinking_params)
        max_output_tokens = self._request_output_limit(
            messages, None, thinking=thinking, thinking_params=thinking_params,
        )
        if _summary_output_limit is not None:
            max_output_tokens = min(max_output_tokens, _summary_output_limit(messages))
        start = time.monotonic()
        protected_call = self.state.wind_down_done
        usage_accounting = _CompletionUsageAccounting(self, protected_call=protected_call)

        def revalidate() -> int:
            limit = self._revalidate_request(
                messages, None, thinking=thinking, thinking_params=thinking_params,
            )
            return min(limit, _summary_output_limit(messages)) if _summary_output_limit is not None else limit

        admitted, scope = self._add_completion_controls(
            self.llm.complete, kwargs, max_output_tokens=max_output_tokens,
            usage_accounting=usage_accounting, revalidate=revalidate,
        )
        if scope is not None:
            messages = scope.prepare_messages(messages)
        abandoned = False
        if self.tracer:
            self.tracer.log_step(step_type="llm_call_started", payload={
                "aid": self.state.aid, "purpose": "summary", "session_step": self.state.step_count,
                "role": getattr(self.agent, "role", None) or getattr(self.agent, "label", None) or self.agent.model,
                "response_session_id": self._response_session_id,
            })

        def account_response(response: CompletionResponse) -> None:
            input_tokens, _total_tokens = _normalize_completion_usage(response.usage)
            usage_accounting.finish(
                response.usage, purpose="summary", late=abandoned,
                response=response,
            )
            self.state.add_markup_recovered(getattr(response.usage, "markup_recovered", 0))
            self.state.set_context_tokens(input_tokens)
            self.record_llm_trace(response, time.monotonic() - start, purpose="summary", thinking=thinking)

        async def complete_owned() -> CompletionResponse:
            try:
                if self._run_control is not None:
                    self._raise_if_run_cancelled()
                return await _complete_with_error_usage(
                    self, complete, messages, usage_accounting=usage_accounting,
                    usage_purpose="summary", on_response=account_response, **kwargs,
                )
            finally:
                self._draining_provider_tasks.discard(asyncio.current_task())
                self._finish_run_control_if_quiesced()

        operation = complete_owned()
        if scope is not None or _native_completion_controls.get() is not None:
            operation = _with_native_completion_controls(operation, scope)
        owner = asyncio.create_task(operation)
        self._track_provider_task(owner)
        try:
            if self._per_call_timeout is None:
                await asyncio.wait({owner})
                return owner.result()
            return await abandon_on_timeout(
                owner, self._per_call_timeout,
                late_task_tracker=self._mark_provider_task_draining,
                late_result_handler=self._provider_task_done,
                start_event=admitted,
            )
        except CallerTimeoutError as exc:
            abandoned = True
            raise GenerationTimeoutError(
                f"LLM summary generation exceeded the {self._per_call_timeout}s per-call timeout"
            ) from exc
        except asyncio.CancelledError:
            abandoned = True
            if not owner.done():
                if self._per_call_timeout is None:
                    owner.cancel()
                self._mark_provider_task_draining(owner)
            raise

    async def _invoke_llm(self, *, max_output_tokens: int, **kwargs: Any) -> CompletionResponse:
        """Bound one provider generation by ``_per_call_timeout``.

        ``None`` disables the ceiling. Expiry raises ``GenerationTimeoutError``
        and tracks unfinished provider work for cleanup and late usage accounting.
        """
        await self._start_llm_step()
        if self._run_control is not None:
            self._raise_if_run_cancelled()
        protected_call = self.state.wind_down_done
        usage_accounting = _CompletionUsageAccounting(self, protected_call=protected_call)
        complete = self.llm.complete
        admitted, scope = self._add_completion_controls(
            complete, kwargs, max_output_tokens=max_output_tokens,
            usage_accounting=usage_accounting,
            revalidate=lambda: self._revalidate_request(
                kwargs["messages"], kwargs.get("tools"), thinking=kwargs.get("thinking", False),
                thinking_params=kwargs.get("thinking_params"),
            ),
        )
        if scope is not None:
            kwargs["messages"] = scope.prepare_messages(kwargs["messages"])
        late_accounted = False

        async def complete_owned() -> CompletionResponse:
            nonlocal late_accounted
            try:
                response = await _complete_with_error_usage(
                    self, complete, usage_accounting=usage_accounting, **kwargs,
                )
                late_accounted = asyncio.current_task() in self._draining_provider_tasks
                usage_accounting.finish(
                    response.usage, purpose="completion", late=late_accounted,
                    response=response,
                )
                return response
            finally:
                self._draining_provider_tasks.discard(asyncio.current_task())
                self._finish_run_control_if_quiesced()

        def record_abandoned(task: asyncio.Task[Any]) -> None:
            if late_accounted:
                self._provider_task_done(task)
            else:
                self._record_late_provider_result(task, accounting=usage_accounting)

        operation = complete_owned()
        if scope is not None or _native_completion_controls.get() is not None:
            operation = _with_native_completion_controls(operation, scope)
        try:
            response = await operation if self._per_call_timeout is None else await abandon_on_timeout(
                operation,
                self._per_call_timeout,
                task_tracker=self._track_provider_task,
                late_task_tracker=self._mark_provider_task_draining,
                late_result_handler=record_abandoned,
                start_event=admitted,
            )
        except CallerTimeoutError as exc:
            raise GenerationTimeoutError(
                f"LLM generation exceeded the {self._per_call_timeout}s per-call timeout"
            ) from exc
        self._context_pressure.observe_request(kwargs["messages"], response.usage)
        return response

    async def _start_llm_step(self) -> None:
        """Record one step immediately before the first provider attempt."""
        if self._llm_step_started:
            return
        self.state.advance_step()
        await self.event_publisher.emit(
            self.event_factory.step_start(self.state.step_count)
        )
        if self.tracer:
            self.tracer.log_step(
                step_type="llm_call_started",
                payload={
                    "aid": self.state.aid,
                    "role": getattr(self.agent, "role", None)
                    or getattr(self.agent, "label", None)
                    or self.agent.model,
                    "session_step": self.state.step_count,
                    "response_session_id": self._response_session_id,
                },
                tokens=0,
                latency=0.0,
            )
            flush = getattr(self.tracer, "flush", None)
            if callable(flush):
                flush()
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
        if response.finish_reason in {"length", "max_tokens", "model_context_window_exceeded"}:
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
        self._context_pressure.observe_output(assistant_msg, response.usage)
        self.state.append_message(assistant_msg)

    async def finish_step(self, latency: float) -> None:
        await self.event_publisher.emit(self.event_factory.step_end(self.state.step_count, latency))
