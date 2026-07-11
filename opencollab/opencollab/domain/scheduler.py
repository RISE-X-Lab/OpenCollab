"""Pure domain for process scheduling — no I/O, no asyncio.

SessionControlBlock (SCB) is the analogue of an OS PCB:
- aid: agent ID (like pid)
- parent_aid: who spawned this agent
- agent: the Agent configuration (model, tools, prompt)
- state: SessionState (messages, tokens, steps, phase)

SessionTable is the scheduler's registry of all SCBs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState


def lead_reserve(total: int) -> int:
    """Headroom the Lead (agent 0) keeps when children are spawned.

    This is the pool the parent retains for its own coordination turns; it is
    the seed value of the scheduler's running ``_allocated_tokens`` so the very
    first child is granted from ``total - lead_reserve(total)``.
    """
    return max(10_000, total // 4)


def split_budget(total: int, allocated: int) -> int:
    """How many tokens a spawned agent gets, given the budget already handed out.

    ``allocated`` is the sum of budget already reserved against the global pool
    (the Lead reserve plus every live child's granted cap). The grant is the
    unallocated remainder ``total - allocated``, floored at 10_000. Flooring can
    push the *sum* of grants past ``total`` only in the exhausted tail (each
    starving child still gets the 10_000 minimum); above the floor the running
    sum of grants never exceeds ``total``.

    Kept pure: all running-total bookkeeping (add on spawn, reclaim on terminal)
    lives in the application-layer scheduler.
    """
    remaining = total - allocated
    return max(10_000, remaining)


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
        # Structured verdict (ref: claude-code review plugin) — the line
        # must equal "VERDICT: PASS" verbatim to avoid false positives
        # from words like "password" containing "PASS".
        passed = any(
            line.strip().upper() == "VERDICT: PASS"
            for line in review_text.splitlines()
        )
        return cls(passed=passed, raw_text=review_text)


@dataclass
class SessionControlBlock:
    """A process control block for an agent session.

    Contains: aid, parent_aid, agent config, state snapshot, result.
    The full Session with SessionRuntime lives elsewhere (managed by Scheduler).
    """

    aid: int
    parent_aid: int | None
    agent: Agent
    state: SessionState
    result: str = ""


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

    def all_done(self) -> bool:
        return all(scb.state.phase.is_terminal() for scb in self.entries.values())


__all__ = [
    "DelegationTask",
    "ReviewVerdict",
    "SessionControlBlock",
    "SessionTable",
    "lead_reserve",
    "split_budget",
]
