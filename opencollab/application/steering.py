"""Closed-loop steering — the per-turn reads-without-write nudge.

Pure functions with no dependency on the runner or ``session_run``, so the run
loop reads top-to-bottom: it gathers the live counters, asks steering for the
block + any ``tool_choice`` force, then folds/persists (see
``SessionRunUseCase.run_llm_call`` for the integration point). Keeping the tool
vocabulary out (``has_write`` / ``has_structured_output`` / ``structured_override``
are computed by the caller) makes this module import-free and unit-testable on
plain values.
"""

from __future__ import annotations

import os
from typing import Any

# Reads-without-write escalation thresholds: at SOFT we advise a write, at HARD
# we demand it and force a tool call.
READS_NUDGE_SOFT = 8
READS_NUDGE_HARD = 16

# Cadence of the budget status line. ``every-step`` repeats it on every turn;
# ``thresholds`` emits it only on the turn that crosses one of
# ``BUDGET_NUDGE_THRESHOLDS_FRACTIONS`` (once per band, since spend is
# monotone); ``off`` never emits it. The reads-without-write rungs are
# unaffected by all three.
BUDGET_NUDGE_EVERY_STEP = "every-step"
BUDGET_NUDGE_THRESHOLDS = "thresholds"
BUDGET_NUDGE_OFF = "off"
BUDGET_NUDGE_MODES = (
    BUDGET_NUDGE_EVERY_STEP,
    BUDGET_NUDGE_THRESHOLDS,
    BUDGET_NUDGE_OFF,
)
BUDGET_NUDGE_ENV_VAR = "OPENCOLLAB_BUDGET_NUDGE_MODE"
BUDGET_NUDGE_THRESHOLDS_FRACTIONS = (0.2, 0.4, 0.6, 0.8)


def resolve_budget_nudge_mode(env: Any = None) -> str:
    """Return the configured cadence, defaulting to ``every-step``.

    Unset, blank, or unrecognised values fall back to ``every-step`` so that a
    run with no environment override behaves exactly as before this knob existed.
    """
    source = os.environ if env is None else env
    raw = str(source.get(BUDGET_NUDGE_ENV_VAR, "") or "").strip().lower()
    return raw if raw in BUDGET_NUDGE_MODES else BUDGET_NUDGE_EVERY_STEP


def _crosses_budget_threshold(
    used_tokens: int, prev_used_tokens: int, max_budget_tokens: int
) -> bool:
    """Return whether spend moved from below to at-or-above a threshold band.

    Crossing, not exceeding: with ``prev`` the spend at the previous turn, a
    band fires on the single turn that steps over its fraction and never again.
    """
    if max_budget_tokens <= 0:
        return False
    previous = min(max(prev_used_tokens, 0), used_tokens)
    frac = used_tokens / max_budget_tokens
    prev_frac = previous / max_budget_tokens
    return any(
        prev_frac < fraction <= frac for fraction in BUDGET_NUDGE_THRESHOLDS_FRACTIONS
    )


def build_steering_block(
    *,
    used_tokens: int,
    max_budget_tokens: int,
    step_count: int,
    max_steps: int,
    reads: int,
    has_write: bool,
    has_structured_output: bool,
    structured_override: Any,
    write_landed: bool = False,
    budget_nudge_mode: str = BUDGET_NUDGE_EVERY_STEP,
    prev_used_tokens: int = 0,
) -> tuple[dict[str, Any] | None, Any | None, str | None]:
    """Build the per-turn steering message + any ``tool_choice`` force.

    Returns ``(message, tool_choice_override_or_None, level)`` where ``level`` is
    ``'hard'`` / ``'soft'`` / ``None`` — the trace seam reads it to log upward
    crossings. The message is a lean ``role:"user"`` block carrying budget
    self-awareness plus, when the session can edit and has read without writing, a
    write nudge (soft) or a hard demand (``tool_choice="required"``). Under the
    default cadence the message is always built; on a fresh post-user turn
    ``reads`` is ~0 so only the status line is returned, which is correct.

    ``structured_override`` is the ``tool_choice`` value that forces the
    structured-output tool — the caller owns the tool vocabulary; steering only
    decides *when* to force it.

    ``budget_nudge_mode`` sets the cadence of the status line only (see
    ``BUDGET_NUDGE_MODES``); the write nudges are unchanged by it. Under
    ``thresholds`` the line rides along only on the turn whose spend crosses a
    band, which needs ``prev_used_tokens`` — the spend at the previous turn.
    When the mode suppresses the line and there is no nudge to carry, the
    message is ``None`` and the caller adds nothing to the turn.
    """
    total = max_budget_tokens or 0
    remaining_k = max(0, total - used_tokens) // 1000
    total_k = total // 1000
    steps_left = max(0, max_steps - step_count)
    status = f"[Budget: ~{remaining_k}k/{total_k}k tokens left, ~{steps_left} steps left.]"
    if budget_nudge_mode == BUDGET_NUDGE_OFF:
        status = ""
    elif budget_nudge_mode == BUDGET_NUDGE_THRESHOLDS and not _crosses_budget_threshold(
        used_tokens, prev_used_tokens, total
    ):
        status = ""

    override: Any | None = None
    level: str | None = None
    extra = ""
    needs_write = has_write and not write_landed
    needs_structured_submit = has_structured_output and (
        write_landed or not has_write
    )
    if needs_write and reads >= READS_NUDGE_HARD:
        extra = (
            f" You have read {reads} times without making an edit. STOP reading"
            " — your next action MUST be a file_write or apply_patch edit."
        )
        override = "required"
        level = "hard"
    elif needs_structured_submit and reads >= READS_NUDGE_SOFT:
        extra = (
            f" You have read {reads} times without submitting structured output."
            " STOP reading — your next action MUST be structured_output using"
            " the evidence you already have."
        )
        override = structured_override
        level = "hard"
    elif needs_write and reads >= READS_NUDGE_SOFT:
        extra = (
            f" You have read {reads} times without making an edit. If"
            " you can describe the fix, make it now with file_write or"
            " apply_patch before reading more."
        )
        level = "soft"
    content = status + extra if status else extra.lstrip()
    if not content:
        return None, override, level
    return {"role": "user", "content": content}, override, level


def fold_steering(last_user_msg: dict[str, Any], steering_text: str) -> dict[str, Any]:
    """Return a copy of a trailing ``user`` message with the steering line folded
    into its content (string concat, or content-part append for the provider list
    form). The original dict is not mutated.
    """
    content = last_user_msg.get("content")
    if isinstance(content, str):
        folded = f"{content}\n\n{steering_text}" if content else steering_text
    elif isinstance(content, list):
        folded = [*content, {"type": "text", "text": steering_text}]
    else:
        folded = steering_text
    return {**last_user_msg, "content": folded}
