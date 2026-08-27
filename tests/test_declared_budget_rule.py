"""A declared team divides its tokens by rule, not by reservation.

When the roster is an outcome of the run, the scheduler has no N to divide the
token pool by, so it reserves as it goes: each agent takes one
``dynamic_roster_share`` — a quarter of the pool — out of the pool the moment it
is created, and keeps it whether or not it ever spends a token. Two consequences
followed. A team could not have more than four agents, because the fourth
emptied the pool. And an agent that was created and then sat idle still held a
quarter of the team's tokens away from the agents that were working, so "the
model never used this role" and "this role spent its allowance" left the same
trace in the accounts.

``prebuild_team`` seats every declared role before the first model call, so N is
known at startup and the pool does not have to be divided blind. These tests pin
what that buys: one shared pool, a per-agent ceiling of ``c * total / N`` with
``c = PER_AGENT_BUDGET_SHARE``, the same ceiling for agent 0 as for everyone
else, nothing frozen by an idle agent, and a record whenever that ceiling — and
not an empty pool — is what refused a turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.steering import build_steering_block
from opencollab.domain.scheduler import (
    PER_AGENT_BUDGET_SHARE,
    dynamic_roster_share,
    per_agent_cap,
)
from opencollab.domain.session import SessionPhase, SessionState

TOTAL = 600_000
ROLES = ("lead", "coder", "tester")
CAP = per_agent_cap(TOTAL, len(ROLES))


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    """The little a scheduler needs of a session to do budget arithmetic."""

    def __init__(self, role: str, budget: int):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.max_budget_tokens = budget
        self.used_tokens = 0

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return "done"


class RecordingFactory:
    """Records the budget every session is built with."""

    def __init__(self) -> None:
        self.lead_budget: int | None = None
        self.spawn_budgets: list[int] = []

    def create_lead_session(self, *, scheduler, launch, budget, aid=0):
        self.lead_budget = budget
        return FakeSession("lead", budget)

    def build_spawn_session(self, *, role, env, budget, aid=-1, **kwargs):
        self.spawn_budgets.append(budget)
        session = FakeSession(role, budget)
        session.state.aid = aid
        return session


class _NoWorktrees:
    async def acquire(self, role):
        return None

    async def release(self):
        return None


class RecordingTracer:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict[str, Any]]] = []

    def log_step(self, *, step_type: str, payload: dict[str, Any]) -> None:
        self.steps.append((step_type, payload))

    def payloads(self, step_type: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.steps if name == step_type]


def _scheduler(*, prebuild_team: bool, tracer: RecordingTracer | None = None):
    factory = RecordingFactory()
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=_NoWorktrees(),
        event_sink=EventBus(None),
        tracer=tracer,
        max_budget_tokens=TOTAL,
        roles=ROLES,
        prebuild_team=prebuild_team,
    )
    lead = factory.create_lead_session(
        scheduler=scheduler, launch=None, budget=scheduler._entry_start_budget()
    )
    scheduler.register_lead(lead)
    return scheduler, factory


def _spend(scheduler: Scheduler, aid: int, tokens: int) -> None:
    scheduler.table.get(aid).state.used_tokens = tokens


# --- the rule ----------------------------------------------------------------


def test_the_cap_is_the_share_constant_times_an_equal_split():
    scheduler, _ = _scheduler(prebuild_team=True)
    assert scheduler._declared_team_size() == 3
    assert CAP == int(TOTAL * PER_AGENT_BUDGET_SHARE / 3) == 300_000
    assert scheduler._per_agent_cap() == CAP
    # Half again an equal split: what one agent may spend past total / N is
    # exactly what makes an idle teammate's allowance reachable.
    assert CAP > TOTAL // 3


def test_every_seat_gets_the_same_cap_agent_zero_included():
    """Agent 0's allowance is the rule's, not a privilege of being the root."""

    async def scenario():
        scheduler, factory = _scheduler(prebuild_team=True)
        assert await scheduler.ensure_team_prebuilt() == (1, 2)
        caps = [scheduler._sessions[aid].max_budget_tokens for aid in (0, 1, 2)]
        assert caps == [CAP, CAP, CAP]
        assert factory.spawn_budgets == [CAP, CAP]

    run(scenario())


def test_agent_zero_no_longer_leases_the_whole_pool_for_a_turn():
    """The privilege itself, and where it survives.

    On the dynamic-roster path agent 0's turn is leased every unreserved token
    while a teammate is held to one share — there is no N, so there is no cap to
    hold agent 0 to either. Under a declared roster both take the same lease.
    """

    async def scenario():
        declared, _ = _scheduler(prebuild_team=True)
        await declared.ensure_team_prebuilt()
        assert declared._entry_agent_takes_the_pool(0) is False
        assert declared._reserve_turn_lease(0) == CAP < TOTAL

        dynamic, _ = _scheduler(prebuild_team=False)
        assert dynamic._entry_agent_takes_the_pool(0) is True
        assert dynamic._reserve_turn_lease(0) == TOTAL

    run(scenario())


def test_agent_zero_is_told_the_budget_it_can_actually_spend():
    """The number in the prompt is the number the scheduler will enforce.

    ``create_lead_session`` used to be handed the team total while the scheduler
    let agent 0 spend a quarter of it, and the session turns whatever it was
    given into the ``[Budget: ...]`` line the model reads on every turn. The two
    have to be one number or the run lies to the model once per turn.
    """
    scheduler, factory = _scheduler(prebuild_team=True)
    assert factory.lead_budget == CAP == scheduler._per_agent_cap()

    block, _override, _level = build_steering_block(
        used_tokens=0,
        max_budget_tokens=scheduler._sessions[0].max_budget_tokens,
        step_count=0,
        max_steps=50,
        reads=0,
        has_write=False,
        has_structured_output=False,
        structured_override=None,
    )
    assert block["content"] == "[Budget: ~300k/300k tokens left, ~50 steps left.]"


# --- what the shared pool buys ----------------------------------------------


def test_an_idle_teammate_freezes_nothing_for_the_agent_doing_the_work():
    """The whole point of sharing the pool instead of splitting it.

    Two of the three seated agents never spend a token. The one that is working
    can still be leased past ``total / N``, and the team's committed total counts
    only what was actually spent — the idle pair commit nothing.
    """

    async def scenario():
        scheduler, _ = _scheduler(prebuild_team=True)
        await scheduler.ensure_team_prebuilt()

        equal_split = TOTAL // len(ROLES)
        _spend(scheduler, 0, equal_split)  # already at an equal share
        assert scheduler.allocated_tokens == equal_split

        grant = scheduler._reserve_turn_lease(0)
        assert grant == CAP - equal_split == 100_000
        assert scheduler._sessions[0].max_budget_tokens == CAP > equal_split
        # Only spend is committed; the two idle seats hold nothing.
        assert scheduler.allocated_tokens == equal_split

    run(scenario())


def test_the_cap_still_bounds_the_agent_that_is_working():
    async def scenario():
        scheduler, _ = _scheduler(prebuild_team=True)
        await scheduler.ensure_team_prebuilt()
        _spend(scheduler, 0, CAP)
        assert scheduler._reserve_turn_lease(0) == 0
        # The pool is far from empty — the cap is what stopped it.
        assert scheduler.used_tokens == CAP < TOTAL

    run(scenario())


# --- the record --------------------------------------------------------------


def test_a_turn_the_cap_refused_is_recorded_with_the_pool_beside_it():
    async def scenario():
        tracer = RecordingTracer()
        scheduler, _ = _scheduler(prebuild_team=True, tracer=tracer)
        await scheduler.ensure_team_prebuilt()
        _spend(scheduler, 0, CAP)
        assert scheduler._reserve_turn_lease(0) == 0

        assert tracer.payloads("agent_cap_reached") == [
            {
                "agent_id": 0,
                "role": "lead",
                "step_count": 0,
                "requested": TOTAL - CAP,
                "per_agent_cap": CAP,
                "used": CAP,
                "remaining": 0,
                "pool_remaining": TOTAL - CAP,
                "spent": CAP,
                "total": TOTAL,
                "team_size": 3,
                "share": PER_AGENT_BUDGET_SHARE,
                "would_exceed_by": TOTAL - CAP,
            }
        ]

    run(scenario())


def test_an_empty_pool_is_not_recorded_as_a_cap_refusal():
    """The record has to mean one thing: this agent, not the team, ran out.

    With the pool itself spent there is nothing the cap withheld, and a record
    here would make an exhausted team look like a throttled agent.
    """

    async def scenario():
        tracer = RecordingTracer()
        scheduler, _ = _scheduler(prebuild_team=True, tracer=tracer)
        await scheduler.ensure_team_prebuilt()
        _spend(scheduler, 0, CAP)
        _spend(scheduler, 1, CAP)
        assert scheduler.used_tokens == TOTAL
        assert scheduler._reserve_turn_lease(2) == 0
        assert tracer.payloads("agent_cap_reached") == []

    run(scenario())


def test_a_failing_tracer_never_overturns_the_allocation():
    class Broken(RecordingTracer):
        def log_step(self, *, step_type, payload):
            raise RuntimeError("tracer down")

    async def scenario():
        scheduler, _ = _scheduler(prebuild_team=True, tracer=Broken())
        await scheduler.ensure_team_prebuilt()
        _spend(scheduler, 0, CAP)
        assert scheduler._reserve_turn_lease(0) == 0
        assert scheduler._sessions[0].max_budget_tokens == CAP

    run(scenario())


# --- the dynamic roster is untouched ----------------------------------------


def test_without_a_declared_roster_the_reservation_rule_still_applies():
    scheduler, factory = _scheduler(prebuild_team=False)
    assert scheduler._per_agent_cap() is None
    assert factory.lead_budget == TOTAL
    # Agent 0 books a quarter of the pool at registration, exactly as before.
    assert scheduler._turn_lease[0] == dynamic_roster_share(TOTAL) == 150_000
    assert scheduler.allocated_tokens == 150_000
    assert scheduler._entry_agent_takes_the_pool(0) is True
