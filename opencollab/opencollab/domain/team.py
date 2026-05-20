"""Pure team orchestration policy."""

from __future__ import annotations

from dataclasses import dataclass


def split_budget(total: int, used: int) -> int:
    """How many tokens a teammate gets, reserving headroom for the Lead."""
    remaining = max(10_000, total - used)
    reserve_for_lead = min(
        max(10_000, total // 4),
        max(0, remaining - 10_000),
    )
    return max(10_000, remaining - reserve_for_lead)


@dataclass(frozen=True)
class DelegationTask:
    """A task the Lead hands to a teammate, with optional preamble context."""

    role: str
    task: str
    context: str = ""

    def render(self) -> str:
        if self.context:
            return f"Context:\n{self.context}\n\nTask:\n{self.task}"
        return self.task


@dataclass(frozen=True)
class ReviewVerdict:
    """A reviewer's PASS/FAIL judgement on a delegated implementation."""

    passed: bool
    raw_text: str

    @classmethod
    def parse(cls, review_text: str) -> "ReviewVerdict":
        # Structured verdict (ref: claude-code review plugin) — the line
        # must equal "VERDICT: PASS" verbatim to avoid false positives
        # from words like "password" containing "PASS".
        passed = any(
            line.strip().upper() == "VERDICT: PASS"
            for line in review_text.splitlines()
        )
        return cls(passed=passed, raw_text=review_text)


__all__ = ["DelegationTask", "ReviewVerdict", "split_budget"]
