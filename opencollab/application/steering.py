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

from typing import Any

# Reads-without-write escalation thresholds. SOFT advises an edit and HARD makes
# the advice urgent while preserving the source tools needed for a safe edit.
READS_NUDGE_SOFT = 8
READS_NUDGE_HARD = 16


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
    failed_edit_path: str | None = None,
) -> tuple[dict[str, Any], Any | None, str | None]:
    """Build the per-turn steering message + any ``tool_choice`` force.

    Returns ``(message, tool_choice_override_or_None, level)`` where ``level`` is
    ``'hard'`` / ``'soft'`` / ``None`` — the trace seam reads it to log upward
    crossings. The message is a lean ``role:"user"`` block carrying budget
    self-awareness plus, when the session can edit and has read without writing, a
    write nudge. At the hard threshold the wording becomes urgent, while the
    model keeps every tool available so it can refresh stale source before an
    edit. The message
    is always built; on a fresh post-user turn ``reads`` is ~0 so only the status
    line is returned, which is correct.

    ``structured_override`` is the ``tool_choice`` value that forces the
    structured-output tool — the caller owns the tool vocabulary; steering only
    decides *when* to force it.
    """
    total = max_budget_tokens or 0
    remaining_k = max(0, total - used_tokens) // 1000
    total_k = total // 1000
    steps_left = max(0, max_steps - step_count)
    status = f"[Budget: ~{remaining_k}k/{total_k}k tokens left, ~{steps_left} steps left.]"

    override: Any | None = None
    level: str | None = None
    extra = ""
    if has_write and reads >= READS_NUDGE_HARD and failed_edit_path:
        extra = (
            f" Your previous edit of {failed_edit_path} failed because its content"
            " anchor was stale. Use file_read or grep on that exact path once,"
            " then retry the edit immediately."
        )
        override = "required"
        level = "hard"
    elif has_write and reads >= READS_NUDGE_HARD:
        extra = (
            f" You have read {reads} times without making an edit. Prioritize the"
            " edit now. If the exact source is uncertain, reread only the range"
            " needed to make that edit safely."
        )
        level = "hard"
    elif has_write and reads >= READS_NUDGE_SOFT:
        extra = (
            f" You have read {reads} times without making an edit. If"
            " you can describe the fix, make it now with file_write or"
            " apply_patch before reading more."
        )
        level = "soft"
    elif has_structured_output and reads >= READS_NUDGE_SOFT:
        extra = (
            f" You have read {reads} times without submitting structured output."
            " STOP reading — your next action MUST be structured_output using"
            " the evidence you already have."
        )
        override = structured_override
        level = "hard"
    return {"role": "user", "content": status + extra}, override, level


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
