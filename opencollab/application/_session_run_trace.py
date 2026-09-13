"""Session context, steering and provider lifecycle trace emission."""

from __future__ import annotations

from typing import Any

from opencollab.application.ports import CompletionResponse
from opencollab.application.shaping import ShaperPipeline
from opencollab.application.steering import READS_NUDGE_SOFT


class _SessionRunTraceMixin:
    """Observation helpers composed into session completion."""

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
                "role": getattr(self.agent, "role", None)
                or getattr(self.agent, "label", None)
                or self.agent.model,
                "session_step": self.state.step_count,
                "response_session_id": self._response_session_id,
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
