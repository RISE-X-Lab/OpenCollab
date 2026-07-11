"""Global-budget enforcement for the team scheduler.

Two mechanisms keep the global token pool a *true* ceiling (it used to be
enforced only per-session against each session's own cap, so fan-out could
oversubscribe it 2-3x):

1. Reserve-at-allocation — each spawned child is granted budget from the
   *unallocated remainder* (``_max_budget_tokens - _allocated_tokens``), and the
   grant is booked synchronously before any await. The sum of grants therefore
   never exceeds the global pool. A terminal child's reservation is reclaimed
   so a later spawn can reuse the headroom.
2. Aggregate runtime ceiling — a session's precheck stops the session once the
   *team aggregate* spend reaches the global cap, regardless of per-session caps.
"""

from __future__ import annotations

import asyncio

import pytest
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.scheduler import SessionControlBlock, lead_reserve
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


class _FailingEventPublisher:
    async def emit(self, event):
        raise RuntimeError("event sink failed")


class _BlockingAcquirePool:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def acquire(self, role):
        self.started.set()
        await self.release.wait()

    async def cleanup(self):
        return None


def _register_budget_lead(sched: Scheduler, total: int) -> None:
    lead = BlockingChild("lead", total, asyncio.Event())
    lead.state.set_phase(SessionPhase.DONE)
    sched.register_lead(lead)


def _scheduler(factory: RecordingFactory, *, max_budget_tokens: int) -> Scheduler:
    sched = Scheduler(
        session_factory=factory,
        worktree_pool=_NoopWorktreePool(),
        event_sink=EventBus(None),
        max_budget_tokens=max_budget_tokens,
    )
    _register_budget_lead(sched, max_budget_tokens)
    return sched


def test_concurrent_grants_never_exceed_global_pool():
    """N children spawned against a global cap: the sum of granted caps plus the
    Lead reserve never exceeds the global pool."""

    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        # Three children receive bounded quarter-shares. The fourth is rejected.
        await sched.spawn(0, "coder", "task-0", tool_call_id="c-0")
        await sched.spawn(0, "coder", "task-1", tool_call_id="c-1")
        await sched.spawn(0, "coder", "task-2", tool_call_id="c-2")
        with pytest.raises(RuntimeError, match="fully allocated"):
            await sched.spawn(0, "coder", "task-3", tool_call_id="c-3")

        grants = factory.grants
        assert grants == [100_000, 100_000, 100_000]
        # The allocation tracker reflects every booked grant.
        assert sched.allocated_tokens == lead_reserve(total) + sum(grants)
        assert sched.allocated_tokens <= total
        assert sched.inflight_spawn("coder", "task-3") is None

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
        assert factory.grants[0] == 100_000
        assert sched.allocated_tokens == lead_reserve(total) + 100_000

        # Finish the first child — its 100_000 reservation must be reclaimed.
        gate.set()
        await sched._tasks[aid0]
        assert sched.allocated_tokens == lead_reserve(total)  # back to 100_000

        # A fresh spawn gets the reclaimed fair share again.
        gate.clear()
        await sched.spawn(0, "coder", "second", tool_call_id="c-1")
        assert factory.grants[1] == 100_000

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
        _register_budget_lead(sched, total)

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
        # refused and is granted from the reclaimed fair-share headroom.
        good = RecordingFactory(gate)
        sched._session_factory = good
        sched._worktree_pool = _NoopWorktreePool()

        aid = await sched.spawn(0, "coder", "retry-me", tool_call_id="c-1")
        assert good.grants[0] == lead_reserve(total)  # 100_000 fair share
        assert sched.inflight_spawn("coder", "retry-me") == aid

        gate.set()
        await asyncio.gather(*sched._tasks.values())

    run(scenario())


def test_spawn_event_failure_rolls_back_all_child_state():
    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = Scheduler(
            session_factory=factory,
            worktree_pool=_NoopWorktreePool(),
            event_sink=_FailingEventPublisher(),
            max_budget_tokens=total,
        )
        lead = BlockingChild("lead", total, gate)
        lead.state.set_phase(SessionPhase.DONE)
        sched.register_lead(lead)

        with pytest.raises(RuntimeError, match="event sink failed"):
            await sched.spawn(0, "coder", "ghost", tool_call_id="child-call")

        assert set(sched.table.entries) == {0}
        assert set(sched._sessions) == {0}
        assert sched._spawn_origin == {}
        assert sched._tasks == {}
        assert sched.inflight_spawn("coder", "ghost") is None
        assert sched.allocated_tokens == lead_reserve(total)

    run(scenario())


def test_cancelled_spawn_rolls_back_reservations_before_driver_exists():
    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        pool = _BlockingAcquirePool()
        factory = RecordingFactory(gate)
        sched = Scheduler(
            session_factory=factory,
            worktree_pool=pool,
            event_sink=EventBus(None),
            max_budget_tokens=total,
        )
        _register_budget_lead(sched, total)

        task = asyncio.create_task(
            sched.spawn(0, "coder", "cancelled", tool_call_id="cancelled-call")
        )
        await pool.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert set(sched.table.entries) == {0}
        assert set(sched._sessions) == {0}
        assert sched._spawn_origin == {}
        assert sched._tasks == {}
        assert sched.inflight_spawn("coder", "cancelled") is None
        assert sched.allocated_tokens == lead_reserve(total)

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


def test_consumed_child_tokens_are_not_reclaimed_as_fresh_headroom():
    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        aid = await sched.spawn(0, "coder", "spend")
        grant = factory.grants[0]
        factory.children[aid].state.add_used_tokens(grant)
        gate.set()
        await sched._tasks[aid]

        # The lease is gone, while its consumed tokens stay committed.
        assert aid not in sched._child_reservation
        assert sched.allocated_tokens == lead_reserve(total) + grant

        gate.clear()
        await sched.spawn(0, "coder", "next-1")
        await sched.spawn(0, "coder", "next-2")
        with pytest.raises(RuntimeError, match="fully allocated"):
            await sched.spawn(0, "coder", "no-double-spend")
        gate.set()
        await asyncio.gather(*sched._tasks.values(), return_exceptions=True)

    run(scenario())


def test_lead_yields_unused_turn_lease_before_parallel_children():
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
        lead = BlockingChild("lead", total, gate)
        lead.state.set_phase(SessionPhase.DONE)
        sched.register_lead(lead)

        assert sched._reserve_turn_budget(0) == total
        lead.state.add_used_tokens(100_000)
        sched._tasks[0] = asyncio.current_task()
        await sched.spawn(0, "coder", "a")
        await sched.spawn(0, "coder", "b")
        await sched.spawn(0, "coder", "c")
        with pytest.raises(RuntimeError, match="fully allocated"):
            await sched.spawn(0, "coder", "d")
        sched._tasks.pop(0)

        assert sched.allocated_tokens == total
        gate.set()
        await asyncio.gather(*sched._tasks.values(), return_exceptions=True)

    run(scenario())


def test_message_revival_reacquires_child_budget_before_starting_turn():
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
        lead = BlockingChild("lead", total, gate)
        lead.state.set_phase(SessionPhase.DONE)
        sched.register_lead(lead)

        aid = await sched.spawn(0, "coder", "first")
        gate.set()
        await sched._tasks[aid]
        gate.clear()

        await sched.send_message(0, aid, "again", "continue")
        assert aid in sched._child_reservation
        assert not sched._tasks[aid].done()

        await sched.spawn(0, "coder", "second")
        await sched.spawn(0, "coder", "third")
        with pytest.raises(RuntimeError, match="fully allocated"):
            await sched.spawn(0, "coder", "fourth")

        assert sched.allocated_tokens == total
        gate.set()
        await asyncio.gather(*sched._tasks.values(), return_exceptions=True)

    run(scenario())


def test_cancelled_agent_retries_other_messages_waiting_for_budget():
    async def scenario():
        total = 400_000
        gate = asyncio.Event()
        factory = RecordingFactory(gate)
        sched = _scheduler(factory, max_budget_tokens=total)

        running = [
            await sched.spawn(0, "coder", f"occupy-{index}")
            for index in range(3)
        ]
        assert sched.allocated_tokens == total

        target = BlockingChild("reviewer", 0, gate)
        target.state.aid = 99
        target.state.set_phase(SessionPhase.DONE)
        sched.table.add(
            SessionControlBlock(
                aid=99,
                parent_aid=0,
                agent=target.agent,
                state=target.state,
            )
        )
        sched._sessions[99] = target

        await sched.send_message(0, 99, "review", "resume when budget frees")
        assert 99 not in sched._tasks
        assert sched._message_inbox[99]

        await asyncio.sleep(0)
        sched._tasks[running[0]].cancel()
        with pytest.raises(asyncio.CancelledError):
            await sched._tasks[running[0]]

        assert 99 in sched._tasks
        assert 99 in sched._child_reservation
        assert sched._message_inbox[99] == []

        gate.set()
        await asyncio.gather(*sched._tasks.values(), return_exceptions=True)

    run(scenario())
