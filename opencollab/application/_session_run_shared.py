"""Shared types and configuration for session execution."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from opencollab.application.ports import CompletionResponse

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})


class GenerationTimeoutError(asyncio.TimeoutError):
    """A single provider generation exceeded its per-call ceiling."""


class _ContextOverflowStop(Exception):
    """Internal control-flow signal: the prompt still overflows the model window
    after a forced maximal compaction pass and one retry. Caught inside
    ``run_llm_call`` to perform a controlled graceful stop (CONTEXT_OVERFLOW)
    rather than letting it propagate as an unhandled ERROR. Never escapes the
    use case.
    """


class _TokenBudgetStop(Exception):
    """Internal signal that a request has no reserved output headroom."""

    def __init__(self, *, reserved_input_tokens: int, remaining_budget: int) -> None:
        self.reserved_input_tokens = reserved_input_tokens
        self.remaining_budget = remaining_budget
        super().__init__(
            "conservative input reservation requires "
            f"{reserved_input_tokens} of {remaining_budget} remaining tokens"
        )


@dataclass
class PendingStep:
    """The LLM response carried across HANDLING_RESPONSE → EXECUTING_TOOLS →
    AUTOSAVING. Lives here, not in domain ``SessionState``: the response is an
    adapter-shaped object and would breach the inward dependency rule.
    """

    response: CompletionResponse
    latency: float
    started_at: float | None = None


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

# Closed-loop steering (the reads-without-write nudge) lives in
# ``application/steering.py``; the tool vocabulary it keys on stays here because
# wind-down shares it. ``READS_NUDGE_SOFT`` is imported above for the trace seam's
# re-arm check.
_READ_TOOLS = frozenset({"file_read", "grep"})
_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
_STRUCTURED_OUTPUT_TOOL = "structured_output"

# Enforcement strength (STEP 0). ``off`` is the self-regulating default: every
# wind-down branch below is gated on enforcement being on, so with ``off`` the
# FSM is byte-for-byte identical to the pre-enforcement code. ``needs-enforcement``
# turns on the structural commit brake for budget-myopic models.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_ON = "needs-enforcement"

# Default name of the structured submit tool the wind-down narrows the toolset to
# (overridable per session). Kept as a string so this application module need not
# import the tool itself.
SUBMIT_TOOL_NAME = "submit_findings"

# Reserve (carved FROM the cap, never additive) held back so the single protected
# submit turn fits: ``explore_threshold = max_budget_tokens - DEFAULT_COMMIT_RESERVE``.
DEFAULT_COMMIT_RESERVE = 25_000

# Progress watchdog + low-yield brake (STEP 3). Two ADDITIONAL triggers into the
# SAME forced-commit actuator (``_enter_wind_down``) — the budget-threshold
# wind-down is the first. Both catch a scout spinning while budget is still
# plentiful (the real pathology) and route it to the non-degradable tool-removal
# commit. They key STRICTLY on the STEP-1 information-gain sensor (consecutive
# no-progress steps / low-yield results), never on ``has_write`` — which is
# overloaded as "agent mutates the repo" and would leak into verify/diff/integrity
# accounting if reused as the brake key.
#
# WATCHDOG_K: ``steps_since_progress`` (no-progress tool STEPS) reaching this trips
# the brake. LOW_YIELD_M: ``low_yield_since_progress`` (consecutive low-yield tool
# RESULTS) reaching this trips it. Both default conservative so a normal
# distill-as-you-read scout is never braked mid-stride.
DEFAULT_WATCHDOG_K = 4
DEFAULT_LOW_YIELD_M = 3
DEFAULT_LOOP_BLOCKED_LIMIT = 3

# Injected as a system message the instant a scout crosses the explore threshold.
# Its next action must be the structured submit; cite-or-abstain, never fabricate.
# STEP 2A (Phase 2): the toolset is narrowed to submit-only AT this same trip, so
# the notice states it UP FRONT — without it a DeepSeek-class model reflexively
# re-issues its prior exploration tool (grep/file_read/bash, now unknown) and burns
# a whole turn on the "unknown tool" error before the provider-compat retry
# (``_WIND_DOWN_RETRY``) recovers it. Telling the model here pre-empts that dead
# turn; the retry backstop stays as the second line of defense.
_WIND_DOWN_NUDGE = (
    "Exploration budget spent — all exploration tools (grep/file_read/bash) have now "
    "been removed; ONLY submit_findings is available. Your NEXT action MUST be "
    "submit_findings with the evidence you already have; do not call any other tool. "
    "Cite file:line / exact matched strings from your real tool results; mark each "
    "finding verified|unverified; if you lack evidence for a dimension, set "
    "insufficient_evidence — do NOT fabricate."
)

# Provider-compat backstop (re-injected for the SINGLE wind-down retry). A model
# that ignored the forced tool_choice and reflexively re-issued its prior tool
# (grep/bash/file_read — now unknown, since the toolset is submit-only) gets one
# more turn with this explicit instruction before the loop goes terminal.
_WIND_DOWN_RETRY = (
    "Only submit_findings is available. Call submit_findings now with the evidence you "
    "have; do not call any other tool. Cite file:line / exact matched strings; mark each "
    "finding verified|unverified; set insufficient_evidence if you lack evidence — do NOT "
    "fabricate."
)


def _submit_tool_choice(name: str) -> dict[str, Any]:
    """OpenAI-style named-function ``tool_choice`` forcing exactly ``name``.

    Constrained decoding for the wind-down turn: more precise than the bare
    ``"required"`` string and more likely honoured by stricter OpenAI-compatible
    endpoints. It rides through the LLM stack unchanged (the OpenAI SDK accepts a
    dict ``tool_choice``); if an endpoint still 400-rejects it, ``_complete``
    degrades it ONCE to ``"auto"`` and the wind-down retry (text instruction +
    submit-only toolset) is the remaining guarantee.
    """
    return {"type": "function", "function": {"name": name}}


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
