"""Global-budget enforcement for the team scheduler.

Two mechanisms keep the global token pool a *true* ceiling (it used to be
enforced only per-session against each session's own cap, so fan-out could
oversubscribe it 2-3x):

1. Reserve-at-allocation — each spawned child is granted budget from the
   *unallocated remainder* (``_max_budget_tokens - _allocated_tokens``), and the
   grant is booked synchronously before any await. The sum of grants therefore
   never exceeds the global pool (above the 10_000 floor). A terminal child's
   reservation is reclaimed so a later spawn can reuse the headroom.
2. Aggregate runtime ceiling — a session's precheck stops the session once the
   *team aggregate* spend reaches the global cap, regardless of per-session caps.
"""

from __future__ import annotations

import asyncio

from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.scheduler import lead_reserve
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


class BlockingChild:
    """A child whose run_loop blocks on a gate so in-flight state is observable.

    Records the budget it was built with so tests can assert the granted caps.
    """

    def __init__(self, role: str, budget: int, gate: asyncio.Event):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.budget = budget
        self._gate = gate

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        await self._gate.wait()
        self.state.set_phase(SessionPhase.DONE)
        self.state.append_message({"role": "assistant", "content": "done"})
        return "done"


class RecordingFactory:
    """Builds a fresh ``BlockingChild`` per spawn, recording each granted budget."""

    def __init__(self, gate: asyncio.Event):
        self._gate = gate
        self.children: dict[int, BlockingChild] = {}
        self.grants: list[int] = []

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        child = BlockingChild(role, budget, self._gate)
        child.state.aid = aid
        self.children[aid] = child
        self.grants.append(budget)
        return child


class _NoopWorktreePool:
    async def acquire(self, role):
        return None

    async def release(self):
        return None


class _RaisingWorktreePool:
    """A pool whose acquire always raises — simulates a spawn that fails after
    the two reservations are booked but before the driver task is scheduled."""

    async def acquire(self, role):
        raise RuntimeError("worktree acquire failed")

    async def release(self):
        return None


class _RaisingFactory(RecordingFactory):
    """Builds children normally, but raises inside build_spawn_session — the
    second site (after acquire) where a spawn can fail post-reservation."""

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        raise RuntimeError("session build failed")


def _scheduler(factory: RecordingFactory, *, max_budget_tokens: int) -> Scheduler:
    sched = Scheduler(
        session_factory=factory,
        worktree_pool=_NoopWorktreePool(),
        event_sink=EventBus(None),
        max_budget_tokens=max_budget_tokens,
    )
    # Seed the Lead reservation the way register_lead would (no real lead session
    # needed for these budget-arithmetic tests).
    sched._seed_lead_reservation()
    return sched


def test_concurrent_grants_never_exceed_global_pool():
    """N children spawned against a global cap: the sum of granted caps plus the
    Lead reserve never exceeds the global pool (above the 10_000 floor)."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        # Spawn 3 children in the same batch (no await between reservations from
        # the model's perspective; each spawn books its grant synchronously).
        for i in range(3):
            await sched.spawn(0, "coder", f"task-{i}", tool_call_id=f"c-{i}")

        grants = factory.grants
        assert len(grants) == 3
        # First grant = pool - lead_reserve; the rest come from the shrinking
        # remainder. Once the pool is allocated, further grants hit the 10_000
        # floor — but the *running allocation* never lets a non-floor grant push
        # the sum past the pool.
        assert grants[0] == total - lead_reserve(total)  # 300_000
        # The allocation tracker reflects every booked grant.
        assert sched.allocated_tokens == lead_reserve(total) + sum(grants)
        # Non-floor grants never oversubscribe: the first child alone fits within
        # the pool, and no second non-floor grant was issued.
        non_floor = [g for g in grants if g > 10_000]
        assert lead_reserve(total) + sum(non_floor) <= total
        # All later grants are the floor (pool already fully allocated).
        assert grants[1:] == [10_000, 10_000]

        gate.set()
        await asyncio.gather(*sched._tasks.values())

    run(scenario())


def test_finished_child_reservation_is_reclaimed():
    """A child that reaches a terminal phase frees its reservation so a later
    spawn is granted from the reclaimed headroom, not the floor."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        aid0 = await sched.spawn(0, "coder", "first", tool_call_id="c-0")
        assert factory.grants[0] == 300_000
        assert sched.allocated_tokens == lead_reserve(total) + 300_000  # 400_000

        # Finish the first child — its 300_000 reservation must be reclaimed.
        gate.set()
        await sched._tasks[aid0]
        assert sched.allocated_tokens == lead_reserve(total)  # back to 100_000

        # A fresh spawn now gets the full reclaimed headroom again, not the floor.
        gate.clear()
        await sched.spawn(0, "coder", "second", tool_call_id="c-1")
        assert factory.grants[1] == 300_000

        gate.set()
        await asyncio.gather(*[t for t in sched._tasks.values()])

    run(scenario())


def test_failed_worktree_acquire_releases_reservations():
    """If ``_worktree_pool.acquire`` raises after the inflight + budget
    reservations are booked, ``spawn`` must release BOTH and re-raise — otherwise
    the budget grant leaks (shrinking the pool forever) and the inflight key
    permanently refuses any re-spawn of the same (role, task)."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = Scheduler(
            session_factory=factory,
            worktree_pool=_RaisingWorktreePool(),
            event_sink=EventBus(None),
            max_budget_tokens=total,
        )
        sched._seed_lead_reservation()

        before = sched.allocated_tokens
        assert before == lead_reserve(total)
        assert sched.inflight_spawn("coder", "leaky") is None

        # The failing spawn must propagate the original exception.
        raised = False
        try:
            await sched.spawn(0, "coder", "leaky", tool_call_id="c-0")
        except RuntimeError as exc:
            raised = True
            assert "worktree acquire failed" in str(exc)
        assert raised, "original exception must propagate out of spawn()"

        # Budget reservation released — allocation is back to its pre-spawn value.
        assert sched.allocated_tokens == before
        # Inflight key cleared — a re-spawn of the SAME (role, task) is not refused.
        assert sched.inflight_spawn("coder", "leaky") is None
        # No driver task was scheduled for the failed spawn.
        assert sched._tasks == {}

    run(scenario())


def test_failed_session_build_releases_reservations_then_respawn_succeeds():
    """The second post-reservation failure site — ``build_spawn_session`` raising
    — must also release both reservations, so a subsequent spawn of the same
    (role, task) books fresh from the reclaimed headroom (not the floor)."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = _RaisingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        before = sched.allocated_tokens

        raised = False
        try:
            await sched.spawn(0, "coder", "retry-me", tool_call_id="c-0")
        except RuntimeError as exc:
            raised = True
            assert "session build failed" in str(exc)
        assert raised
        assert sched.allocated_tokens == before
        assert sched.inflight_spawn("coder", "retry-me") is None

        # Swap in a working factory: a re-spawn of the SAME (role, task) is NOT
        # refused and is granted from the full reclaimed headroom, not the floor.
        good = RecordingFactory(gate)
        sched._session_factory = good
        sched._worktree_pool = _NoopWorktreePool()

        aid = await sched.spawn(0, "coder", "retry-me", tool_call_id="c-1")
        assert good.grants[0] == total - lead_reserve(total)  # 300_000, not floor
        assert sched.inflight_spawn("coder", "retry-me") == aid

        gate.set()
        await asyncio.gather(*sched._tasks.values())

    run(scenario())


def test_successful_spawn_does_not_double_release():
    """Regression: a NORMAL spawn holds its reservation until the agent
    terminates — the failure-path release must not fire on the success path."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        before = sched.allocated_tokens
        aid = await sched.spawn(0, "coder", "work", tool_call_id="c-0")

        # Reservation is HELD while the child runs (not released by the success
        # path of spawn) — allocation reflects the live grant.
        assert sched.allocated_tokens == before + factory.grants[0]
        assert sched.inflight_spawn("coder", "work") == aid

        # The child terminates: the driver task releases exactly once.
        gate.set()
        await sched._tasks[aid]
        assert sched.allocated_tokens == before  # reclaimed once, not twice
        assert sched.inflight_spawn("coder", "work") is None

    run(scenario())


def test_lead_reservation_seeded_at_registration():
    """register_lead seeds the running allocation with the Lead reserve."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = Scheduler(
            session_factory=factory,
            worktree_pool=_NoopWorktreePool(),
            event_sink=EventBus(None),
            max_budget_tokens=total,
        )
        assert sched.allocated_tokens == 0  # nothing reserved yet

        lead = BlockingChild("lead", total, gate)
        sched.register_lead(lead)
        assert sched.allocated_tokens == lead_reserve(total)

    run(scenario())


def test_budget_exhausted_reflects_aggregate_spend():
    """The aggregate ceiling predicate flips once team total reaches the cap."""

    async def scenario():
        total = 100_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        assert sched.budget_exhausted is False

        # Spawn a child and simulate spend that reaches the global cap.
        aid = await sched.spawn(0, "coder", "burn", tool_call_id="c-0")
        child = factory.children[aid]
        child.state.add_used_tokens(total)  # aggregate now == cap
        assert sched.used_tokens >= total
        assert sched.budget_exhausted is True

        gate.set()
        await asyncio.gather(*sched._tasks.values())

    run(scenario())
