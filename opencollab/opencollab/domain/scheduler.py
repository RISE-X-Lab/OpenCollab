"""Pure domain for process scheduling — no I/O, no asyncio.

SessionControlBlock (SCB) is the analogue of an OS PCB:
- aid: agent ID (like pid)
- parent_aid: who spawned this agent
- agent: the Agent configuration (model, tools, prompt)
- state: SessionState (messages, tokens, steps, phase)

ProcessTable is the scheduler's registry of all SCBs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState


def split_budget(total: int, used: int) -> int:
    """How many tokens a spawned agent gets, reserving headroom for the Lead."""
    remaining = max(10_000, total - used)
    reserve_for_lead = min(
        max(10_000, total // 4),
        max(0, remaining - 10_000),
    )
    return max(10_000, remaining - reserve_for_lead)


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
class ProcessTable:
    """The scheduler's registry of all agent processes."""

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

    def children_of(self, parent_aid: int) -> list[SessionControlBlock]:
        return [scb for scb in self.entries.values() if scb.parent_aid == parent_aid]

    @property
    def total_used_tokens(self) -> int:
        return sum(scb.state.used_tokens for scb in self.entries.values())

    def all_done(self) -> bool:
        from opencollab.domain.session import SessionPhase

        return all(
            scb.state.phase
            in {
                SessionPhase.DONE,
                SessionPhase.CANCELLED,
                SessionPhase.BUDGET_EXCEEDED,
                SessionPhase.ERROR,
            }
            for scb in self.entries.values()
        )


__all__ = [
    "DelegationTask",
    "ProcessTable",
    "ReviewVerdict",
    "SessionControlBlock",
    "split_budget",
]
