"""Shared output truncation for tool results.

Several tools (bash, git_diff, run_tests) bound their stdout/stderr so one huge
result can't blow up the model's context. They all keep a head + tail and drop
the middle; this is the single copy of that rule. ``label`` names the truncated
stream in the marker when given (e.g. ``stdout``).
"""

from __future__ import annotations


def truncate(text: str, max_chars: int, label: str | None = None) -> str:
    """Keep head + tail, drop the middle to avoid context explosion."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    marker = (
        f"\n\n... [{dropped} chars of {label} truncated] ...\n\n"
        if label is not None
        else f"\n\n... [{dropped} chars truncated] ...\n\n"
    )
    if len(marker) >= max_chars:
        return marker[:max_chars]
    source_budget = max_chars - len(marker)
    head = (source_budget + 1) // 2
    tail = source_budget - head
    suffix = text[-tail:] if tail else ""
    result = text[:head] + marker + suffix
    assert len(result) <= max_chars
    return result


__all__ = ["truncate"]
