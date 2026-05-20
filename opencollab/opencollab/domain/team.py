"""Pure team orchestration policy."""

from __future__ import annotations


def split_budget(total: int, used: int) -> int:
    """How many tokens a teammate gets, reserving headroom for the Lead."""
    remaining = max(10_000, total - used)
    reserve_for_lead = min(
        max(10_000, total // 4),
        max(0, remaining - 10_000),
    )
    return max(10_000, remaining - reserve_for_lead)
