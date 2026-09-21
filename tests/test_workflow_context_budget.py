"""Budget and cleanup ownership tests for WorkflowContext."""

from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest
from workflow_context_test_support import (
    CancelCleanupSession,
    FakeFactory,
    FakeSession,
    _DeferredTokenSession,
)

from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.application.workflow import (
    WorkflowBudgetExceeded,
    WorkflowContext,
)
from opencollab.application.workflow_runtime import (
    DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS,
)

# Waits on work that must SUCCEED. It is event-loop bookkeeping with no
# intrinsic duration, so a sub-second budget bounds how starved the machine is
# rather than the code, and reports work that finished as a red build. The
# waits meant to EXPIRE keep their own small timeouts.
MUST_SETTLE_SECONDS = 10.0

# The deadline test below hands the synth a caller deadline and checks that the
# synth is bounded by it. A millisecond deadline cannot do that: it can expire
# while the synth session is still being built, and ``_remaining_timeout`` then
# raises before ``run_loop`` is ever called — the synth never starts, and the
# test reads that as a cancellation failure. Half a second is far above the
# scheduling jitter that costs, so the synth is certain to have started.
DEAD_SCOUT_CALLER_TIMEOUT_SECONDS = 0.5

# The bound the deadline is *checked* against is a separate number from the
# hang guard: a guard sized to tell a honoured deadline from the internal commit
# cap would also pass an implementation that overran the caller by a second.
# This one is generous against a loaded machine — half a second of slack on top
# of the deadline, against the scheduling jitter actually measured here (a 10 ms
# deadline was missed once in fifteen runs on a machine held busy) — while still
# failing an overrun long before the internal cap could explain it.
CALLER_DEADLINE_HONOURED_SECONDS = DEAD_SCOUT_CALLER_TIMEOUT_SECONDS + 0.5
assert CALLER_DEADLINE_HONOURED_SECONDS < DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_session_budget_rejects_next_call_after_concurrent_overspend():
    """A call waiting on the semaphore cannot build after the pool is spent."""
    first_running = asyncio.Event()
    release_first = asyncio.Event()

    async def first_on_enter() -> None:
        # First session is built+appended and holds the semaphore; spend pending.
        first_running.set()
        await release_first.wait()

    s1 = _DeferredTokenSession(tokens=150, on_enter=first_on_enter)
    s2 = FakeSession(reply="b", tokens=0)
    factory = FakeFactory([s1, s2])
    # Single permit -> the second agent must wait on the first inside the
    # gate->build window, reproducing the concurrent-overspend race.
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=1)

    # 1. First agent: passes gate (spent==0), takes the permit, parks in run_loop.
    first_task = asyncio.create_task(ctx.agent("first"))
    await first_running.wait()
    assert factory.builds[0]["budget"] == 100  # full budget, nothing spent yet

    # 2. Second agent blocks on the permit before it can acquire a budget lease.
    second_task = asyncio.create_task(ctx.agent("second"))
    for _ in range(5):  # let it clear the gate and park on the semaphore
        await asyncio.sleep(0)
    assert len(factory.builds) == 1  # it has NOT built yet (still gated by permit)

    # 3. Land the first session's spend: spent jumps to 150, remaining == -50.
    s1.land_spend()
    assert ctx.budget.spent() == 150

    # 4. Release the first agent; the second sees the exhausted pool and stops.
    release_first.set()
    with pytest.raises(WorkflowBudgetExceeded):
        await asyncio.wait_for(second_task, timeout=1.0)
    assert await first_task == "a"

    assert len(factory.builds) == 1

@pytest.mark.asyncio
async def test_parallel_agents_atomically_reserve_shared_budget():
    release = asyncio.Event()
    sessions = [
        FakeSession(reply="a", tokens=60, gate=release),
        FakeSession(reply="b", tokens=40, gate=release),
    ]
    factory = FakeFactory(sessions)
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=2)

    task = asyncio.create_task(
        ctx.parallel(
            [
                lambda: ctx.agent("first", budget=60),
                lambda: ctx.agent("second", budget=60),
            ]
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if len(factory.builds) == 2:
            break

    assert [build["budget"] for build in factory.builds] == [60, 40]
    assert ctx.budget.remaining() == 0
    release.set()
    assert await task == ["a", "b"]
    assert ctx.budget.spent() == 100

@pytest.mark.asyncio
@pytest.mark.parametrize("max_concurrency", (1, 2, 3))
async def test_uncapped_parallel_agents_split_budget_before_concurrency_admission(
    max_concurrency,
):
    release = asyncio.Event()
    sessions = [FakeSession(reply=str(i), gate=release) for i in range(3)]
    factory = FakeFactory(sessions)
    ctx = WorkflowContext(factory, budget_total=90, max_concurrency=max_concurrency)

    task = asyncio.create_task(
        ctx.parallel([lambda i=i: ctx.agent(f"agent {i}") for i in range(3)])
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if factory.builds:
                break

        assert factory.builds[0]["budget"] == 30
        release.set()
        assert await task == ["0", "1", "2"]
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)

    grants = [build["budget"] for build in factory.builds]
    assert grants == [30, 30, 30]
    assert sum(grants) <= 90


@pytest.mark.asyncio
async def test_collection_never_starts_zero_budget_sessions():
    release = asyncio.Event()
    sessions = [
        FakeSession(reply=str(index), tokens=1, gate=release)
        for index in range(3)
    ]
    factory = FakeFactory(sessions)
    ctx = WorkflowContext(factory, budget_total=2, max_concurrency=3)
    task = asyncio.create_task(
        ctx.parallel([lambda i=i: ctx.agent(f"agent {i}") for i in range(3)])
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if len(factory.builds) >= 2:
                break
    finally:
        release.set()

    with pytest.raises(WorkflowBudgetExceeded):
        await task

    grants = [build["budget"] for build in factory.builds]
    assert grants == [1, 1]
    assert sum(grants) == 2


@pytest.mark.asyncio
async def test_parallel_cancellation_releases_bounded_worker_budget_lease():
    started = asyncio.Event()
    gate = asyncio.Event()

    async def first_on_enter() -> None:
        started.set()
        await gate.wait()

    sessions = [
        FakeSession(gate=gate, on_enter=first_on_enter),
        FakeSession(),
        FakeSession(),
    ]
    ctx = WorkflowContext(
        FakeFactory(sessions),
        budget_total=90,
        max_concurrency=3,
        task_concurrency=1,
    )
    task = asyncio.create_task(
        ctx.parallel([lambda i=i: ctx.agent(f"agent {i}") for i in range(3)])
    )

    await started.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    # Bounded collection scheduling starts only one task worker,
    # while preserving the planned one-third budget grant.
    assert len(ctx.budget._leases) == 1
    assert ctx.budget.remaining() == 60

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ctx.wait_for_pending_cleanup()

    assert ctx.budget.remaining() == 90


@pytest.mark.asyncio
async def test_timeout_keeps_budget_reserved_until_cancel_cleanup_finishes():
    timed_out = CancelCleanupSession(tokens_after_cancel=80)
    second_gate = asyncio.Event()
    second = FakeSession(reply="second", gate=second_gate)
    factory = FakeFactory([timed_out, second])
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=2)

    assert await ctx.agent("slow", budget=80, timeout=0.05) is None
    await timed_out.cancel_seen.wait()

    second_task = asyncio.create_task(ctx.agent("second", budget=80))
    for _ in range(20):
        await asyncio.sleep(0)
        if len(factory.builds) == 2:
            break

    assert factory.builds[1]["budget"] == 20
    timed_out.release_cancel.set()
    second_gate.set()
    assert await second_task == "second"
    for _ in range(20):
        await asyncio.sleep(0)
        if ctx.budget.spent() == 80:
            break
    assert ctx.budget.spent() == 80

@pytest.mark.asyncio
async def test_pending_cleanup_wait_covers_unreserved_over_budget_lease():
    timed_out = CancelCleanupSession(tokens_after_cancel=0)
    ctx = WorkflowContext(FakeFactory([timed_out]), budget_total=0)

    assert (
        await ctx.agent("forced", timeout=0.05, over_budget_ok=True) is None
    )
    await timed_out.cancel_seen.wait()

    waiter = asyncio.create_task(ctx.wait_for_pending_cleanup())
    await asyncio.sleep(0)
    assert waiter.done() is False
    timed_out.release_cancel.set()
    await waiter


@pytest.mark.asyncio
async def test_over_budget_escape_is_single_use():
    """Only one exhausted-pool call may use the forced-write escape."""
    factory = FakeFactory(
        [FakeSession(reply="forced"), FakeSession(reply="must not run")]
    )
    ctx = WorkflowContext(factory, budget_total=0)

    assert await ctx.agent("forced write", over_budget_ok=True) == "forced"
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.agent("second forced write", over_budget_ok=True)

    assert len(factory.builds) == 1


@pytest.mark.asyncio
async def test_concurrent_over_budget_escapes_allow_only_one_call():
    """The one-shot escape is claimed atomically when calls race."""
    release = asyncio.Event()
    factory = FakeFactory([FakeSession(reply="forced", gate=release)])
    ctx = WorkflowContext(factory, budget_total=0, max_concurrency=2)

    tasks = [
        asyncio.create_task(ctx.agent(f"forced-{index}", over_budget_ok=True))
        for index in range(2)
    ]
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if len(factory.builds) == 1:
                break
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(isinstance(result, WorkflowBudgetExceeded) for result in results) == 1
    assert sum(isinstance(result, str) and result == "forced" for result in results) == 1


@pytest.mark.asyncio
async def test_timeout_keeps_concurrency_slot_until_cancel_cleanup_finishes():
    active = 0
    overlapped = False

    class TimedSession(CancelCleanupSession):
        async def run_loop(self, cancel_event=None):
            nonlocal active
            active += 1
            try:
                return await super().run_loop(cancel_event)
            finally:
                active -= 1

    async def enter_second() -> None:
        nonlocal overlapped
        overlapped = active != 0

    timed_out = TimedSession()
    second = FakeSession(reply="second", on_enter=enter_second)
    factory = FakeFactory([timed_out, second])
    ctx = WorkflowContext(factory, max_concurrency=1)

    assert await ctx.agent("slow", timeout=0.05) is None
    await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)

    second_task = asyncio.create_task(ctx.agent("second"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(factory.builds) == 1
    assert second_task.done() is False

    timed_out.release_cancel.set()
    assert await asyncio.wait_for(second_task, timeout=MUST_SETTLE_SECONDS) == "second"
    assert overlapped is False

@pytest.mark.asyncio
async def test_active_background_agent_is_visible_to_boundary_owner():
    started = asyncio.Event()
    release = asyncio.Event()

    async def enter() -> None:
        started.set()

    session = FakeSession(gate=release, on_enter=enter)
    ctx = WorkflowContext(FakeFactory([session]))
    agent_task = asyncio.create_task(ctx.agent("background"))

    await asyncio.wait_for(started.wait(), timeout=MUST_SETTLE_SECONDS)
    assert agent_task in ctx.pending_cleanup_tasks

    release.set()
    assert await asyncio.wait_for(agent_task, timeout=MUST_SETTLE_SECONDS) == "done"
    await asyncio.sleep(0)
    assert ctx.pending_cleanup_tasks == ()

@pytest.mark.asyncio
async def test_pending_cleanup_callback_consumes_late_exception():
    class FailingCancelCleanupSession(CancelCleanupSession):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def run_loop(self, cancel_event=None):
            self.entered.set()
            try:
                return await super().run_loop(cancel_event)
            except asyncio.CancelledError:
                raise RuntimeError("late cleanup failure") from None

    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    session = FailingCancelCleanupSession()
    ctx = WorkflowContext(FakeFactory([session]))
    try:
        call = asyncio.create_task(ctx.agent("slow", timeout=1.0))
        await asyncio.wait_for(session.entered.wait(), timeout=MUST_SETTLE_SECONDS)
        assert await call is None
        await asyncio.wait_for(session.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)
        session.release_cancel.set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)

@pytest.mark.asyncio
async def test_enforced_timeout_does_not_start_synth_while_scout_cleans_up():
    timed_out = CancelCleanupSession()
    timed_out.state.turn.scout_ledger = [
        {
            "tool": "read_file",
            "target": "module.py",
            "outcome": "ok",
            "snippet": "evidence",
        }
    ]
    synth = FakeSession(reply="must not overlap")
    factory = FakeFactory([timed_out, synth])
    ctx = WorkflowContext(factory, max_concurrency=1)

    result = await ctx.agent(
        "scout",
        timeout=0.05,
        enforcement_strength=ENFORCEMENT_ON,
    )

    assert result is not None and "evidence cards" in result
    assert len(factory.builds) == 1
    await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)
    timed_out.release_cancel.set()
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)


@pytest.mark.asyncio
async def test_dead_scout_synth_respects_the_callers_deadline():
    class DeadScout(FakeSession):
        async def run_loop(self, cancel_event=None):
            raise RuntimeError("scout failed after collecting evidence")

    class BlockingSynth(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run_loop(self, cancel_event=None):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    scout = DeadScout()
    scout.state.turn.scout_ledger = [
        {
            "tool": "file_read",
            "target": "module.py",
            "outcome": "hit",
            "snippet": "observed evidence",
        }
    ]
    synth = BlockingSynth()
    ctx = WorkflowContext(FakeFactory([scout, synth]))
    started_at = asyncio.get_running_loop().time()
    try:
        result = await asyncio.wait_for(
            ctx.agent(
                "scout",
                timeout=DEAD_SCOUT_CALLER_TIMEOUT_SECONDS,
                enforcement_strength=ENFORCEMENT_ON,
            ),
            # A hang guard, nothing more — the elapsed bound below is what
            # actually holds the implementation to the caller's deadline.
            timeout=MUST_SETTLE_SECONDS,
        )
    finally:
        await ctx.wait_for_pending_cleanup()

    assert result is not None and "evidence cards" in result
    # Separate "the synth never ran" from "the synth ran and the deadline
    # cancelled it": only the second says the deadline reached the synth.
    assert synth.started.is_set()
    assert synth.cancelled.is_set()
    assert asyncio.get_running_loop().time() - started_at < CALLER_DEADLINE_HONOURED_SECONDS


@pytest.mark.asyncio
async def test_dead_scout_synth_uses_internal_cap_without_caller_deadline(monkeypatch):
    dead = FakeSession()
    dead.state.turn.scout_ledger = [
        {
            "tool": "file_read",
            "target": "module.py",
            "outcome": "hit",
            "snippet": "observed evidence",
        }
    ]
    ctx = WorkflowContext(FakeFactory([FakeSession()]))
    internal_deadline = asyncio.get_running_loop().time() + 120.0
    seen_deadlines: list[float | None] = []

    monkeypatch.setattr(ctx, "_internal_commit_deadline", lambda: internal_deadline)

    async def record_deadline(_session, _prompt, *, deadline, cancel_event=None):
        seen_deadlines.append(deadline)
        return ""

    monkeypatch.setattr(ctx, "_run_session_turn", record_deadline)

    await ctx._synthesize_dead_scout(
        dead,
        "scout",
        commit_reserve=1,
        caller_deadline=None,
    )

    assert seen_deadlines == [internal_deadline]


@pytest.mark.asyncio
async def test_caller_cancellation_keeps_budget_reserved_until_cleanup_finishes():
    cancelled = CancelCleanupSession(tokens_after_cancel=80)
    second_gate = asyncio.Event()
    second = FakeSession(reply="second", gate=second_gate)
    factory = FakeFactory([cancelled, second])
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=2)

    second_task: asyncio.Task | None = None
    try:
        first_task = asyncio.create_task(
            ctx.agent("cancel me", budget=80, timeout=10.0)
        )
        await cancelled.started.wait()
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await cancelled.cancel_seen.wait()

        second_task = asyncio.create_task(ctx.agent("second", budget=80))
        for _ in range(20):
            await asyncio.sleep(0)
            if len(factory.builds) == 2:
                break

        assert factory.builds[1]["budget"] == 20
    finally:
        cancelled.release_cancel.set()
        second_gate.set()
        if second_task is not None:
            await asyncio.gather(second_task, return_exceptions=True)

    assert second_task is not None
    assert second_task.result() == "second"

@pytest.mark.asyncio
async def test_budget_lease_release_cannot_be_cancelled_while_lock_is_held():
    release_first = asyncio.Event()
    first_started = asyncio.Event()

    async def enter_first() -> None:
        first_started.set()

    first = FakeSession(reply="first", gate=release_first, on_enter=enter_first)
    second = FakeSession(reply="second")
    ctx = WorkflowContext(
        FakeFactory([first, second]),
        budget_total=100,
        max_concurrency=1,
    )

    first_task = asyncio.create_task(ctx.agent("first", budget=80))
    await asyncio.wait_for(first_started.wait(), timeout=MUST_SETTLE_SECONDS)
    await ctx._budget_lock.acquire()
    try:
        release_first.set()
        for _ in range(5):
            await asyncio.sleep(0)
        first_task.cancel()
        assert await asyncio.wait_for(first_task, timeout=MUST_SETTLE_SECONDS) == "first"
        assert ctx.budget._leases == []
    finally:
        ctx._budget_lock.release()

    assert await asyncio.wait_for(
        ctx.agent("second", budget=80),
        timeout=0.5,
    ) == "second"
    assert ctx.budget._leases == []
