"""Pure team allocation rules, delegation values, and session registry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

PER_AGENT_BUDGET_SHARE = 1.0
"""Dimensionless share ``c`` in a declared seat's cap ``c * total / N``.

``c=1`` gives equal seat caps; ``c=N`` permits one seat to consume the pool.
Intermediate values permit partial sharing. This design fixes c before a run.
With a pool N times the solo allowance, c=1 gives every seat that solo allowance,
so spending through only one seat cannot benefit from the larger team pool.
Caps are not creation-time reservations: an idle seat holds no tokens.
Prechecks still permit in-flight overshoot; inspect recorded usage.
"""

def per_agent_cap(
    total: int,
    team_size: int,
    share: float = PER_AGENT_BUDGET_SHARE,
) -> int:
    """Return a nonnegative cumulative seat cap: floor(share * total / team_size).

    Clamp to the whole pool; a nonpositive team size returns max(0, total).
    This is a spend ceiling, not a reservation at seating.
    """
    if team_size <= 0:
        return max(0, total)
    return max(0, min(total, int(total * share / team_size)))

def dynamic_roster_share(total: int) -> int:
    """Return a dynamic agent's quarter-pool share, floored at 10k and capped at total.

    The roster has no known N; declared prebuilt teams use per_agent_cap instead.
    """
    return min(total, max(10_000, total // 4))

def split_budget(total: int, allocated: int) -> int:
    """Grant at most one dynamic share from the unallocated pool; zero if exhausted.

    The application scheduler books this reservation at creation and reclaims it
    on completion. Declared teams use cumulative seat caps instead.
    """
    remaining = max(0, total - allocated)
    return min(dynamic_roster_share(total), remaining)

@dataclass(frozen=True)
class DelegationTask:
    """A task the Lead hands to a spawned agent, with optional preamble context."""

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
        # The review prompt requires the response to END with one exact verdict
        # line. Looking at earlier lines lets quoted instructions or a superseded
        # draft verdict override the reviewer's final judgement.
        lines = [line.strip() for line in review_text.splitlines() if line.strip()]
        passed = bool(
            lines
            and re.fullmatch(
                r"VERDICT: PASS[.!。！]?",
                lines[-1],
                flags=re.IGNORECASE,
            )
        )
        return cls(passed=passed, raw_text=review_text)

@dataclass
class SessionControlBlock:
    """Registry data for an agent; the Scheduler owns its live Session/runtime."""

    aid: int
    parent_aid: int | None
    agent: Agent
    state: SessionState
    result: str = ""
    answer: str | None = None
    provider_error_code: str | None = None
    provider_status_code: int | None = None

@dataclass
class SessionTable:
    """The scheduler's registry of all agent sessions."""

    entries: dict[int, SessionControlBlock] = field(default_factory=dict)
    _next_aid: int = field(default=0, repr=False)

    def allocate_aid(self) -> int:
        aid = self._next_aid
        self._next_aid += 1
        return aid

    def add(self, scb: SessionControlBlock) -> None:
        self.entries[scb.aid] = scb

    def get(self, aid: int) -> SessionControlBlock | None:
        return self.entries.get(aid)

    @property
    def total_used_tokens(self) -> int:
        return sum(scb.state.used_tokens for scb in self.entries.values())

__all__ = [
    "PER_AGENT_BUDGET_SHARE",
    "DelegationTask",
    "ReviewVerdict",
    "SessionControlBlock",
    "SessionTable",
    "dynamic_roster_share",
    "per_agent_cap",
    "split_budget",
]
