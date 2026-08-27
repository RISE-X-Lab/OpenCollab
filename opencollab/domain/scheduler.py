"""Pure domain for process scheduling — no I/O, no asyncio.

SessionControlBlock (SCB) is the analogue of an OS PCB:
- aid: agent ID (like pid)
- parent_aid: who spawned this agent
- agent: the Agent configuration (model, tools, prompt)
- state: SessionState (messages, tokens, steps, phase)

SessionTable is the scheduler's registry of all SCBs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

PER_AGENT_BUDGET_SHARE = 1.5
"""``c`` in the per-agent budget cap ``c * total / N``. Dimensionless.

Every agent on a declared team draws from one shared pool of ``total`` tokens
and may spend at most ``c * total / N`` of it, where ``N`` is the number of
roles the team config declares. Nothing is set aside for an agent when it is
created, so ``c`` is the only thing that decides how unequally the pool may be
consumed:

- ``c = 1`` is a strict equal split. Every agent is capped at ``total / N``, the
  caps sum to exactly the pool, and no agent can spend a token another agent did
  not spend. This is the allocation most multi-agent harnesses implement.
- ``c = N`` is full sharing. Each agent is capped at the whole pool, so the
  first agent to run may consume all of it.
- ``1 < c < N`` lets an agent that is doing the work draw on the allowance of an
  agent that is idle, while still bounding how much of the pool any one agent
  can take.

The value is ``1.5``: one agent may spend up to half again an equal share. It is
a free parameter of the allocation rule, not an estimate of any quantity, and it
is fixed before data collection so the caps are a property of the design rather
than of the runs.

Overspend is bounded, not impossible. The caps sum to ``c * total``, which is
more than the pool, so the caps alone do not bound the team; the aggregate
ceiling does. Every session's precheck tests the team total before it starts a
model call, so once aggregate spend reaches ``total`` no agent begins another
turn. What can still be spent past ``total`` is therefore only what was already
in flight when the ceiling was crossed: at most one turn per agent running
concurrently, and on a pipeline topology, one turn. That is a bound on the order
of a single turn's tokens, not of ``(c - 1) * total``. The realized figure is
recorded per run rather than assumed, so it can be reported.

The alternative this replaces — taking each agent's share out of the pool when
the agent is created — has no overspend at all, but holds tokens for agents that
never spend them, which makes an idle agent and an exhausted one
indistinguishable in the accounts.
"""


def per_agent_cap(
    total: int,
    team_size: int,
    share: float = PER_AGENT_BUDGET_SHARE,
) -> int:
    """The most one agent of a declared team of ``team_size`` may spend.

    ``share * total / team_size``, floored to a whole number of tokens and
    clamped to ``total`` — no single agent may be allowed more than the whole
    pool, which binds whenever ``share >= team_size``.

    A cap on cumulative spend, not a reservation. Seating an agent takes nothing
    out of the pool, so an agent that is created and never used holds no tokens
    and its allowance stays available to the agents that are working. That is
    what keeps "the model never used this role" and "this role spent its whole
    allowance" apart in the data.
    """
    if team_size <= 0:
        return max(0, total)
    return max(0, min(total, int(total * share / team_size)))


def dynamic_roster_share(total: int) -> int:
    """One agent's share of the pool when the roster is discovered at runtime.

    On the dynamic-roster path the team is whatever the model spawns, so the
    number of agents is unknown while the run is still deciding it. With no N to
    divide by, each agent — agent 0 included — books a fixed quarter of the
    pool, floored at 10k. That quarter is also where this path's ceiling of four
    concurrent agents comes from: after the fourth booking the pool is gone.

    When the team config declares the roster up front, N is known before the
    first model call and ``per_agent_cap`` divides by it instead.
    """
    return min(total, max(10_000, total // 4))


def split_budget(total: int, allocated: int) -> int:
    """How many tokens a spawned agent gets, given the budget already handed out.

    ``allocated`` is the sum of budget already reserved against the global pool
    (every live agent's granted cap, agent 0's included). A live agent receives
    at most one ``dynamic_roster_share``, clamped to the unallocated remainder,
    so the running sum stays bounded by ``total``. Once the pool is exhausted,
    the grant is zero.

    Reserve-at-allocation, and therefore the dynamic-roster path only: the grant
    is taken out of the pool when the agent is created, whether or not the agent
    ever spends it. ``per_agent_cap`` is the declared-roster alternative.

    Kept pure: all running-total bookkeeping (add on spawn, reclaim on terminal)
    lives in the application-layer scheduler.
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
