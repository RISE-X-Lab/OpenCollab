"""Shared output truncation for tool results.

Several tools (bash, git_diff, run_tests) bound their stdout/stderr so one huge
result can't blow up the model's context. They all keep a head + tail and drop
the middle; this is the single copy of that rule. ``label`` names the truncated
stream in the marker when given (e.g. ``stdout``).
"""

from __future__ import annotations


def truncate(text: str, max_chars: int, label: str | None = None) -> str:
    """Keep head + tail, drop the middle to avoid context explosion."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    dropped = len(text) - max_chars
    marker = (
        f"\n\n... [{dropped} chars of {label} truncated] ...\n\n"
        if label is not None
        else f"\n\n... [{dropped} chars truncated] ...\n\n"
    )
    return text[:half] + marker + text[-half:]


__all__ = ["truncate"]
