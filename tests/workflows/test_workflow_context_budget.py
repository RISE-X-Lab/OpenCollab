"""Budget and cleanup ownership tests for WorkflowContext."""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any

import pytest

from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.application.workflow import (
    WorkflowBudgetExceeded,
    WorkflowContext,
)
from opencollab.application.workflow_runtime import (
    DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS,
)
from tests.support.workflow_context_test_support import (
    CancelCleanupSession,
    FakeFactory,
    FakeSession,
    _DeferredTokenSession,
)

# Allow event-loop scheduling time for waits that must succeed.
MUST_SETTLE_SECONDS = 10.0

# These cleanup scenarios need a running session before its real caller
# deadline expires. Short preparation deadlines are exercised separately.
CANCEL_CLEANUP_CALLER_TIMEOUT_SECONDS = 0.5

# Leave time to build the synth before its caller deadline tests cancellation.
DEAD_SCOUT_CALLER_TIMEOUT_SECONDS = 0.5

# A separate elapsed bound detects caller-deadline overruns before the internal commit cap.
CALLER_DEADLINE_HONOURED_SECONDS = DEAD_SCOUT_CALLER_TIMEOUT_SECONDS + 0.5
assert CALLER_DEADLINE_HONOURED_SECONDS < DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS

@pytest.mark.asyncio
async def test_session_budget_rejects_next_call_after_concurrent_overspend():
    """A call waiting on the semaphore cannot build after the pool is spent."""
    first_running = asyncio.Event()
    release_first = asyncio.Event()

    async def first_on_enter() -> None:
        first_running.set()
        await release_first.wait()

    s1 = _DeferredTokenSession(tokens=150, on_enter=first_on_enter)
    s2 = FakeSession(reply="b", tokens=0)
    factory = FakeFactory([s1, s2])
    # Single permit -> the second agent must wait on the first inside the
    # gate->build window, reproducing the concurrent-overspend race.
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=1)

    first_task = asyncio.create_task(ctx.agent("first"))
    await first_running.wait()
    assert factory.builds[0]["budget"] == 100

    second_task = asyncio.create_task(ctx.agent("second"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(factory.builds) == 1

    s1.land_spend()
    assert ctx.budget.spent() == 150

    release_first.set()
    with pytest.raises(WorkflowBudgetExceeded):
        await asyncio.wait_for(second_task, timeout=1.0)
    assert await first_task == "a"

    assert len(factory.builds) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["agent", "agent_run"])
async def test_queued_budget_shrinks_without_taking_other_reservations(method):
    started, release = asyncio.Event(), asyncio.Event()

    async def enter_first():
        started.set()
        await release.wait()

    first = _DeferredTokenSession(tokens=15_000, on_enter=enter_first)
    factory = FakeFactory([first, FakeSession(tokens=15), FakeSession(tokens=25)])
    ctx = WorkflowContext(factory, budget_total=30_000, max_concurrency=1)
    run = getattr(ctx, method)
    tasks = [asyncio.create_task(run("first", budget=10_000))]

    async def wait_for_reservations(count):
        while len(ctx.budget._leases) < count:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(started.wait(), MUST_SETTLE_SECONDS)
        for count, prompt in ((2, "second"), (3, "third")):
            tasks.append(asyncio.create_task(run(prompt, budget=10_000)))
            await asyncio.wait_for(wait_for_reservations(count), MUST_SETTLE_SECONDS)
        assert len(factory.builds) == 1
        first.land_spend()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), MUST_SETTLE_SECONDS)
        assert [build["budget"] for build in factory.builds] == [10_000, 5_000, 10_000]
        assert ctx.tokens_spent() == 15_040
        assert ctx.tokens_remaining() == 14_960
        assert ctx.budget._leases == []
        assert ctx._semaphore._value == 1
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.wait_for_pending_cleanup()

@pytest.mark.asyncio
async def test_queued_over_budget_call_claims_the_single_escape():
    started, release = asyncio.Event(), asyncio.Event()

    async def enter_first():
        started.set()
        await release.wait()

    first = _DeferredTokenSession(tokens=21_000, on_enter=enter_first)
    factory = FakeFactory([first, FakeSession(reply="forced", tokens=15)])
    ctx = WorkflowContext(factory, budget_total=20_000, max_concurrency=1)
    tasks = [asyncio.create_task(ctx.agent("first", budget=10_000))]

    async def wait_for_reservations():
        while len(ctx.budget._leases) < 2:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(started.wait(), MUST_SETTLE_SECONDS)
        tasks.append(asyncio.create_task(ctx.agent("forced", budget=10_000, over_budget_ok=True)))
        await asyncio.wait_for(wait_for_reservations(), MUST_SETTLE_SECONDS)
        first.land_spend()
        release.set()
        values = await asyncio.wait_for(asyncio.gather(*tasks), MUST_SETTLE_SECONDS)
        assert values[1] == "forced"
        assert [build["budget"] for build in factory.builds] == [10_000, 10_000]
        with pytest.raises(WorkflowBudgetExceeded):
            await ctx.agent("another escape", over_budget_ok=True)
        assert ctx.budget._leases == []
        assert ctx._semaphore._value == 1
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.wait_for_pending_cleanup()

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
@pytest.mark.parametrize("cancel_index", [1, 2], ids=["first-waiter", "last-waiter"])
async def test_cancelling_equal_budget_waiter_preserves_remaining_allowance(cancel_index):
    started = asyncio.Event()
    release = asyncio.Event()

    async def mark_started():
        started.set()

    factory = FakeFactory([
        FakeSession(tokens=500, gate=release, on_enter=mark_started),
        FakeSession(tokens=100),
        FakeSession(tokens=5),
    ])
    ctx = WorkflowContext(factory, budget_total=1500, max_concurrency=1)
    tasks = [asyncio.create_task(ctx.agent("first", budget=500))]

    async def wait_for_reservations(count):
        while len(ctx.budget._leases) < count:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(started.wait(), MUST_SETTLE_SECONDS)
        for count, label in ((2, "second"), (3, "third")):
            tasks.append(asyncio.create_task(ctx.agent(label, budget=500)))
            await asyncio.wait_for(wait_for_reservations(count), MUST_SETTLE_SECONDS)
        assert len(factory.builds) == 1

        tasks[cancel_index].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[cancel_index]
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(task for index, task in enumerate(tasks) if index != cancel_index)),
            MUST_SETTLE_SECONDS,
        )
        await ctx.wait_for_pending_cleanup()

        assert ctx.tokens_spent() == 600
        assert ctx.tokens_remaining() == 900
        assert ctx.budget._leases == []
        assert await ctx.agent("follow-up", budget=900) == "done"
        assert factory.builds[-1]["budget"] == 900
        assert ctx.tokens_remaining() == 895
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.wait_for_pending_cleanup()

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
    first_task = asyncio.create_task(ctx.agent(
        "slow", budget=80, timeout=CANCEL_CLEANUP_CALLER_TIMEOUT_SECONDS,
    ))
    owners = [first_task]
    try:
        await asyncio.wait_for(timed_out.started.wait(), timeout=MUST_SETTLE_SECONDS)
        assert await asyncio.wait_for(first_task, timeout=MUST_SETTLE_SECONDS) is None
        await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)

        second_task = asyncio.create_task(ctx.agent("second", budget=80))
        owners.append(second_task)
        for _ in range(20):
            await asyncio.sleep(0)
            if len(factory.builds) == 2:
                break

        assert factory.builds[1]["budget"] == 20
        timed_out.release_cancel.set()
        second_gate.set()
        assert await asyncio.wait_for(second_task, timeout=MUST_SETTLE_SECONDS) == "second"
    finally:
        timed_out.release_cancel.set()
        second_gate.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*owners, return_exceptions=True),
            timeout=MUST_SETTLE_SECONDS,
        )
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)
    assert ctx.budget.spent() == 80

@pytest.mark.asyncio
async def test_pending_cleanup_wait_covers_unreserved_over_budget_lease():
    timed_out = CancelCleanupSession(tokens_after_cancel=0)
    ctx = WorkflowContext(FakeFactory([timed_out]), budget_total=0)
    call = asyncio.create_task(ctx.agent(
        "forced", timeout=CANCEL_CLEANUP_CALLER_TIMEOUT_SECONDS, over_budget_ok=True,
    ))
    owners = [call]
    try:
        await asyncio.wait_for(timed_out.started.wait(), timeout=MUST_SETTLE_SECONDS)
        assert await asyncio.wait_for(call, timeout=MUST_SETTLE_SECONDS) is None
        await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)

        waiter = asyncio.create_task(ctx.wait_for_pending_cleanup())
        owners.append(waiter)
        await asyncio.sleep(0)
        assert waiter.done() is False
        timed_out.release_cancel.set()
        await asyncio.wait_for(waiter, timeout=MUST_SETTLE_SECONDS)
    finally:
        timed_out.release_cancel.set()
        if not call.done():
            call.cancel()
        await asyncio.wait_for(
            asyncio.gather(*owners, return_exceptions=True),
            timeout=MUST_SETTLE_SECONDS,
        )
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["build", "prepare"])
@pytest.mark.parametrize("over_budget", [False, True])
async def test_preparation_deadline_stops_before_run_loop_and_returns_budget(stage, over_budget):
    class SlowPreparationSession(CancelCleanupSession):
        async def add_user_message(self, prompt):
            if stage == "prepare":
                await asyncio.sleep(0.08)
            await super().add_user_message(prompt)

    class SlowFactory(FakeFactory):
        def build_workflow_session(self, **kwargs):
            if stage == "build":
                time.sleep(0.08)
            return super().build_workflow_session(**kwargs)

    session = SlowPreparationSession()
    total = 0 if over_budget else 100
    ctx = WorkflowContext(SlowFactory([session]), budget_total=total)
    try:
        assert await asyncio.wait_for(
            ctx.agent("preparation", timeout=0.05, budget=None if over_budget else 80,
                      over_budget_ok=over_budget),
            timeout=MUST_SETTLE_SECONDS,
        ) is None
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)
        assert session.started.is_set() is False
        assert session.cancel_seen.is_set() is False
        assert ctx.pending_cleanup_tasks == ()
        assert ctx.budget._leases == []
        assert ctx.budget.spent() == 0
        assert ctx.budget.remaining() == total
    finally:
        session.release_cancel.set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)

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

    first_task = asyncio.create_task(ctx.agent("slow", timeout=CANCEL_CLEANUP_CALLER_TIMEOUT_SECONDS))
    owners = [first_task]
    try:
        await asyncio.wait_for(timed_out.started.wait(), timeout=MUST_SETTLE_SECONDS)
        assert await asyncio.wait_for(first_task, timeout=MUST_SETTLE_SECONDS) is None
        await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)

        second_task = asyncio.create_task(ctx.agent("second"))
        owners.append(second_task)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(factory.builds) == 1
        assert second_task.done() is False

        timed_out.release_cancel.set()
        assert await asyncio.wait_for(second_task, timeout=MUST_SETTLE_SECONDS) == "second"
        assert overlapped is False
    finally:
        timed_out.release_cancel.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*owners, return_exceptions=True),
            timeout=MUST_SETTLE_SECONDS,
        )
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=MUST_SETTLE_SECONDS)

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

    call = asyncio.create_task(ctx.agent(
        "scout", timeout=CANCEL_CLEANUP_CALLER_TIMEOUT_SECONDS, enforcement_strength=ENFORCEMENT_ON,
    ))
    try:
        await asyncio.wait_for(timed_out.started.wait(), timeout=MUST_SETTLE_SECONDS)
        result = await asyncio.wait_for(call, timeout=MUST_SETTLE_SECONDS)
        assert result is not None and "evidence cards" in result
        assert len(factory.builds) == 1
        await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=MUST_SETTLE_SECONDS)
    finally:
        timed_out.release_cancel.set()
        if not call.done():
            call.cancel()
        await asyncio.wait_for(asyncio.gather(call, return_exceptions=True), timeout=MUST_SETTLE_SECONDS)
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
            # The elapsed assertion below checks the caller deadline independently of this hang timeout.
            timeout=MUST_SETTLE_SECONDS,
        )
    finally:
        await ctx.wait_for_pending_cleanup()

    assert result is not None and "evidence cards" in result
    # Confirm the synth started before checking that the deadline cancelled it.
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
    seen_turns: list[tuple[float | None, str | None]] = []

    monkeypatch.setattr(ctx, "_internal_commit_deadline", lambda: internal_deadline)

    async def record_turn(_session, _prompt, *, deadline, label=None, cancel_event=None):
        seen_turns.append((deadline, label))
        return ""

    monkeypatch.setattr(ctx, "_run_session_turn", record_turn)

    await ctx._synthesize_dead_scout(
        dead,
        "scout",
        commit_reserve=1,
        caller_deadline=None,
    )

    assert seen_turns == [(internal_deadline, "scout:synth")]

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
