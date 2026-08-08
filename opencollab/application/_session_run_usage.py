"""Provider usage validation shared by session completion paths."""

from __future__ import annotations

from typing import Any


def _nonnegative_usage_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"provider usage {field} must be a non-negative integer")
    return value


def _normalize_completion_usage(usage: Any) -> tuple[int, int]:
    """Validate provider counters atomically and prevent total undercharging."""
    input_tokens = _nonnegative_usage_int(
        getattr(usage, "input_tokens", None),
        "input_tokens",
    )
    reported_total = _nonnegative_usage_int(
        getattr(usage, "total_tokens", None),
        "total_tokens",
    )
    raw_output = getattr(usage, "output_tokens", None)
    output_tokens = (
        max(0, reported_total - input_tokens)
        if raw_output is None
        else _nonnegative_usage_int(raw_output, "output_tokens")
    )
    return input_tokens, max(reported_total, input_tokens + output_tokens)
