"""Session context, steering and provider lifecycle trace emission."""

from __future__ import annotations

from typing import Any

from opencollab.application.ports import CompletionResponse
from opencollab.application.shaping import ShaperPipeline
from opencollab.application.shaping.pipeline import context_token_estimator
from opencollab.application.steering import READS_NUDGE_SOFT


class _SessionRunTraceMixin:
    """Observation helpers composed into session completion."""

    async def _shape_and_trace(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Shape a copy of history and record the compaction layers that fired.

        Emit one ``context_shaping`` record per fired layer, or one ``rung="none"``
        record for an untouched turn. Labels come from the shapers for comparison
        with the assigned policy. The pipeline reports changes and this mixin emits.
        """
        # A ShaperPipeline names its own rungs; wrap anything else (a single
        # shaper, or nothing wired at all) so every turn still reports.
        pipeline = (
            self.shaper
            if isinstance(self.shaper, ShaperPipeline)
            else ShaperPipeline(() if self.shaper is None else (self.shaper,))
        )
        with context_token_estimator(self._context_pressure):
            if self.tracer is None and self._run_event_receiver is None:
                return await pipeline.ashape(messages)
            shaped, reports = await pipeline.ashape_with_report(messages)
        for report in reports:
            self._emit_run_event("context_shaping", emergency=False, **report)
            if self.tracer is None:
                continue
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

    def _llm_trace_request_fields(self) -> dict[str, Any]:
        return {
            "session_step": self.state.step_count,
            "response_session_id": self._response_session_id,
            "request_tool_names": list(self._last_request_tool_names),
            "request_tool_choice": self._last_request_tool_choice,
            "request_observation_stage": "application",
        }

    def record_llm_trace(
        self, response: CompletionResponse | None, latency: float, *, purpose: str | None = None,
        thinking: bool | None = None, error: BaseException | None = None, late: bool = False,
        request_fields: dict[str, Any] | None = None,
    ) -> None:
        if self.tracer:
            result = error if error is not None else response
            tool_calls_log = None
            tool_calls = getattr(result, "tool_calls", None)
            if tool_calls:
                tool_calls_log = [
                    {
                        "id": tc.get("id"),
                        "name": tc.get("function", {}).get("name"),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    }
                    for tc in tool_calls
                ]
            usage = getattr(result, "usage", None)
            input_tokens = getattr(usage, "input_tokens", 0)
            total_tokens = getattr(usage, "total_tokens", input_tokens)
            payload = {
                # Join steering and commit-brake records through the same agent id.
                "aid": self.state.aid,
                "model": self.agent.model,
                "finish_reason": getattr(result, "finish_reason", None),
                "content": getattr(result, "content", None),
                "tool_calls": tool_calls_log,
                "role": getattr(self.agent, "role", None)
                or getattr(self.agent, "label", None)
                or self.agent.model,
                **(self._llm_trace_request_fields() if request_fields is None else request_fields),
            }
            if purpose is not None:
                payload["purpose"] = purpose
            if error is not None:
                payload["status"] = "error"
                payload["error"] = {"type": type(error).__name__, "message": str(error)}
            if late:
                payload["late"] = True
            if purpose == "summary":
                payload["request_tool_names"] = []
                payload["request_tool_choice"] = None
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
            reasoning = getattr(result, "reasoning", None)
            if reasoning:
                payload["reasoning"] = reasoning
            elif payload.get("usage", {}).get("reasoning_tokens"):
                # Mark the gap between billed reasoning and returned content
                # so trajectory readers can identify withheld reasoning.
                payload["reasoning_withheld"] = True
            payload["thinking"] = (
                thinking if thinking is not None
                else purpose != "summary" and bool(getattr(self.agent, "thinking", False))
            )
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
            provider_model = getattr(result, "provider_model", None)
            if provider_model is not None:
                payload["provider_model"] = provider_model
            payload["transport_timing"] = getattr(result, "transport_timing", None)
            self.tracer.log_step(
                step_type="llm_call",
                payload=payload,
                tokens=total_tokens,
                latency=latency,
            )

    def record_llm_cancelled(self) -> None:
        if not self.tracer or not self._llm_step_started:
            return
        self.tracer.log_step(
            step_type="llm_call_cancelled",
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
