"""Dynamic team budget remains booked for cancellation-resistant providers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState
from tests.support.session_run_loop_test_support import llm_response


class _Lead:
    def __init__(self) -> None:
        self.agent = SimpleNamespace(name="lead")
        self.state = SessionState(messages=[])
        self.state.set_phase(SessionPhase.DONE)
        self.used_tokens = 0


class _WorktreePool:
    async def acquire(self, _role: str) -> None:
        return None

    async def release(self) -> None:
        return None


class _LateProvider:
    def __init__(self, *, late_error: bool = False, block: bool = False) -> None:
        self.cancel_seen = asyncio.Event()
        self.release_late = asyncio.Event()
        self.started = asyncio.Event()
        self.late_error = late_error
        self.block = block

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        del messages, tools, temperature, kwargs
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_late.wait()
            if self.late_error:
                raise RuntimeError("late provider failure")
            return llm_response(content="late", total_tokens=100_000)


class _Factory:
    def __init__(self) -> None:
        self.providers: dict[int, _LateProvider] = {}
        self.sessions = {}

    def build_spawn_session(self, *, role, budget, aid, **_kwargs):
        provider = _LateProvider(block=aid != 1)
        self.providers[aid] = provider
        session = build_session(
            agent=Agent(name=role, system_prompt="test"),
            llm=provider,
            max_budget_tokens=budget,
            llm_timeout=0.01 if aid == 1 else 30.0,
            aid=aid,
        )
        self.sessions[aid] = session
        return session


def _scheduler(factory: _Factory) -> Scheduler:
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=_WorktreePool(),
        event_sink=EventBus(),
        max_budget_tokens=400_000,
    )
    scheduler.register_lead(_Lead())
    return scheduler


@pytest.mark.asyncio
async def test_late_provider_usage_stays_reserved_after_child_terminal():
    factory = _Factory()
    scheduler = _scheduler(factory)
    first = await scheduler.spawn(0, "coder", "late success")
    first_task = scheduler._tasks[first]
    provider = factory.providers[first]

    try:
        await asyncio.wait_for(provider.cancel_seen.wait(), timeout=0.5)
        await asyncio.wait_for(first_task, timeout=0.5)
        assert scheduler.table.get(first).state.phase is SessionPhase.ERROR
        assert scheduler._turn_lease.get(first) is None
        assert scheduler.allocated_tokens == 200_000
        assert scheduler.inflight_spawn("coder", "late success") is None

        # Two live grants consume the remaining pool. The terminal first child
        # still owns its potential late bill, so a fourth grant must be refused.
        second = await scheduler.spawn(0, "coder", "second live child")
        third = await scheduler.spawn(0, "coder", "third live child")
        assert scheduler.allocated_tokens == 400_000
        with pytest.raises(RuntimeError, match="fully allocated"):
            await scheduler.spawn(0, "coder", "would oversubscribe")

        provider.release_late.set()
        pending = factory.sessions[first].runner.pending_cleanup_tasks
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=0.5
        )
        assert factory.sessions[first].state.used_tokens == 100_000
        assert scheduler.used_tokens == 100_000
        # Late usage replaces the first grant's reservation before it is
        # dropped; the two live siblings still account for the rest of the cap.
        assert scheduler.allocated_tokens == 400_000
        assert scheduler._pending_provider_budget_leases == {}
    finally:
        provider.release_late.set()
        for aid in (first,):
            session = factory.sessions.get(aid)
            if session is not None:
                await asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True)
        for aid in (locals().get("second"), locals().get("third")):
            if aid is not None and aid in scheduler._tasks:
                scheduler._tasks[aid].cancel()
                await asyncio.gather(scheduler._tasks[aid], return_exceptions=True)


@pytest.mark.asyncio
async def test_late_provider_failure_releases_unused_budget_reservation():
    factory = _Factory()
    scheduler = _scheduler(factory)
    first = await scheduler.spawn(0, "coder", "late error")
    first_task = scheduler._tasks[first]
    provider = factory.providers[first]
    provider.late_error = True

    try:
        await asyncio.wait_for(provider.cancel_seen.wait(), timeout=0.5)
        await asyncio.wait_for(first_task, timeout=0.5)
        assert scheduler.allocated_tokens == 200_000

        provider.release_late.set()
        pending = factory.sessions[first].runner.pending_cleanup_tasks
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=0.5
        )
        assert scheduler.used_tokens == 0
        assert scheduler.allocated_tokens == 100_000

        # An error response carries no usage, so the full unused grant returns.
        next_aid = await scheduler.spawn(0, "coder", "after late error")
        assert next_aid != first
        assert scheduler.allocated_tokens == 200_000
        scheduler._tasks[next_aid].cancel()
        await asyncio.gather(scheduler._tasks[next_aid], return_exceptions=True)
    finally:
        provider.release_late.set()
        await asyncio.gather(*factory.sessions[first].pending_cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_grouped_provider_reservation_waits_for_each_accounting_callback():
    factory = _Factory()
    scheduler = _scheduler(factory)
    aid = 9
    state = SessionState(messages=[])
    gate = asyncio.Event()

    async def cleanup_owner():
        await gate.wait()

    owners = (asyncio.create_task(cleanup_owner()), asyncio.create_task(cleanup_owner()))
    scheduler.table.add(
        SessionControlBlock(
            aid=aid,
            parent_aid=0,
            agent=SimpleNamespace(name="coder"),
            state=state,
        )
    )
    scheduler._sessions[aid] = SimpleNamespace(
        runner=SimpleNamespace(pending_cleanup_tasks=owners)
    )
    scheduler._turn_lease[aid] = 100_000
    scheduler._lease_baseline[aid] = 0
    # The session runner registers its late-usage callbacks before the
    # scheduler observes these tasks.
    for owner in owners:
        owner.add_done_callback(lambda _task: state.add_used_tokens(50_000))

    scheduler._release_leases(aid)
    reservation_observations: list[bool] = []
    for owner in owners:
        owner.add_done_callback(
            lambda _task: reservation_observations.append(
                bool(scheduler._pending_provider_budget_leases)
            )
        )

    gate.set()
    await asyncio.gather(*owners)

    assert state.used_tokens == 100_000
    assert reservation_observations == [True, False]
    assert scheduler._pending_provider_budget_leases == {}
