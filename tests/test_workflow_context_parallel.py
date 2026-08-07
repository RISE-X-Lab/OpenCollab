"""Parallel and pipeline tests for WorkflowContext."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from workflow_context_test_support import (
    FakeFactory,
    FakeSession,
)

from opencollab.application.workflow import (
    WorkflowBudgetExceeded,
    WorkflowContext,
)


@pytest.mark.asyncio
async def test_concurrency_cap_honored():
    n = 6
    running = 0
    high_water = 0
    lock = asyncio.Lock()
    gates = [asyncio.Event() for _ in range(n)]

    async def make_on_enter(idx: int) -> Callable[[], Awaitable[None]]:
        async def on_enter() -> None:
            nonlocal running, high_water
            async with lock:
                running += 1
                high_water = max(high_water, running)
            # Let the scheduler admit anyone the semaphore would allow, then
            # release this session so the high-water reflects true concurrency.
            for _ in range(5):
                await asyncio.sleep(0)
            async with lock:
                running -= 1
            gates[idx].set()

        return on_enter

    sessions = [
        FakeSession(
            reply=str(i),
            gate=gates[i],
            on_enter=await make_on_enter(i),
        )
        for i in range(n)
    ]
    factory = FakeFactory(sessions)
    ctx = WorkflowContext(
        factory,
        max_concurrency=2,
        task_concurrency=n,
    )

    thunks = [(lambda i=i: ctx.agent(f"p{i}")) for i in range(n)]
    results = await ctx.parallel(thunks)

    assert sorted(results) == [str(i) for i in range(n)]
    assert high_water <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["parallel", "pipeline"])
@pytest.mark.parametrize("task_concurrency", [1, 2, 4])
async def test_collections_cap_arbitrary_units(composition, task_concurrency):
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def unit(value: int) -> int:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running == task_concurrency:
            admitted.set()
        try:
            await release.wait()
            return value
        finally:
            running -= 1

    ctx = WorkflowContext(
        FakeFactory([]),
        max_concurrency=8,
        task_concurrency=task_concurrency,
    )
    if composition == "parallel":
        operation = ctx.parallel([lambda i=i: unit(i) for i in range(8)])
    else:

        async def stage(_previous, item, _index):
            return await unit(item)

        operation = ctx.pipeline(list(range(8)), stage)
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(admitted.wait(), timeout=0.5)
        for _ in range(10):
            await asyncio.sleep(0)
        assert high_water == task_concurrency
    finally:
        release.set()
    assert await task == list(range(8))


@pytest.mark.asyncio
async def test_task_and_agent_concurrency_caps_are_independent():
    task_running = 0
    agent_running = 0
    task_high_water = 0
    agent_high_water = 0
    combined_high_water = 0
    tasks_admitted = asyncio.Event()
    agents_admitted = asyncio.Event()
    release = asyncio.Event()

    def record_high_water() -> None:
        nonlocal task_high_water, agent_high_water, combined_high_water
        task_high_water = max(task_high_water, task_running)
        agent_high_water = max(agent_high_water, agent_running)
        combined_high_water = max(
            combined_high_water,
            task_running + agent_running,
        )

    async def agent_entered() -> None:
        nonlocal agent_running
        agent_running += 1
        record_high_water()
        if agent_running == 2:
            agents_admitted.set()
        try:
            await release.wait()
        finally:
            agent_running -= 1

    sessions = [
        FakeSession(reply=str(index), on_enter=agent_entered)
        for index in range(3)
    ]
    ctx = WorkflowContext(
        FakeFactory(sessions),
        max_concurrency=2,
        task_concurrency=3,
    )

    async def task_unit(index: int) -> str | None:
        nonlocal task_running
        task_running += 1
        record_high_water()
        if task_running == 3:
            tasks_admitted.set()
        try:
            return await ctx.agent(f"agent {index}")
        finally:
            task_running -= 1

    task = asyncio.create_task(
        ctx.parallel([lambda i=i: task_unit(i) for i in range(3)])
    )
    try:
        await asyncio.wait_for(tasks_admitted.wait(), timeout=0.5)
        await asyncio.wait_for(agents_admitted.wait(), timeout=0.5)
        assert task_high_water == 3
        assert agent_high_water == 2
        assert combined_high_water == 5
    finally:
        release.set()

    assert await task == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_concurrent_collections_share_the_task_concurrency_cap():
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def unit(value: int) -> int:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running == 2:
            admitted.set()
        try:
            await release.wait()
            return value
        finally:
            running -= 1

    async def stage(_previous, item, _index):
        return await unit(item)

    ctx = WorkflowContext(
        FakeFactory([]),
        max_concurrency=8,
        task_concurrency=2,
    )
    task = asyncio.gather(
        ctx.parallel([lambda i=i: unit(i) for i in range(4)]),
        ctx.pipeline(list(range(4, 8)), stage),
    )
    try:
        await asyncio.wait_for(admitted.wait(), timeout=0.5)
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        release.set()

    parallel_result, pipeline_result = await task
    assert high_water == 2
    assert parallel_result == list(range(4))
    assert pipeline_result == list(range(4, 8))


@pytest.mark.asyncio
async def test_direct_nested_collection_reuses_task_permit_at_single_concurrency():
    ctx = WorkflowContext(
        FakeFactory([]),
        max_concurrency=1,
        task_concurrency=1,
    )

    async def nested():
        return await ctx.parallel(
            [
                lambda: asyncio.sleep(0, result="first"),
                lambda: asyncio.sleep(0, result="second"),
            ]
        )

    result = await asyncio.wait_for(
        ctx.parallel([nested]),
        timeout=0.5,
    )

    assert result == [["first", "second"]]


@pytest.mark.asyncio
async def test_gathered_nested_collections_borrow_one_task_permit_serially():
    running = 0
    high_water = 0

    async def unit(value: str) -> str:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        try:
            await asyncio.sleep(0)
            return value
        finally:
            running -= 1

    async def nested():
        async def stage(_previous, item, _index):
            return await unit(item)

        return await asyncio.gather(
            ctx.parallel([lambda: unit("parallel")]),
            ctx.pipeline(["pipeline"], stage),
        )

    ctx = WorkflowContext(
        FakeFactory([]),
        max_concurrency=1,
        task_concurrency=1,
    )
    result = await asyncio.wait_for(
        ctx.parallel([nested]),
        timeout=0.5,
    )

    assert result == [[["parallel"], ["pipeline"]]]
    assert high_water == 1


@pytest.mark.asyncio
async def test_task_permit_released_after_unit_exception():
    entered = asyncio.Event()

    async def raises() -> None:
        raise RuntimeError("unit failed")

    async def succeeds() -> str:
        entered.set()
        return "ok"

    ctx = WorkflowContext(
        FakeFactory([]),
        task_concurrency=1,
    )

    assert await ctx.parallel([raises]) == [None]
    assert await asyncio.wait_for(
        ctx.parallel([succeeds]),
        timeout=0.5,
    ) == ["ok"]
    assert entered.is_set()


@pytest.mark.asyncio
async def test_task_permit_released_after_collection_cancellation():
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def waits_forever() -> None:
        started.set()
        await blocked.wait()

    ctx = WorkflowContext(
        FakeFactory([]),
        task_concurrency=1,
    )
    cancelled = asyncio.create_task(ctx.parallel([waits_forever]))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert await asyncio.wait_for(
        ctx.parallel([lambda: asyncio.sleep(0, result="next")]),
        timeout=0.5,
    ) == ["next"]


@pytest.mark.asyncio
async def test_inherited_closed_task_permit_is_not_reused_by_stale_child():
    child_release = asyncio.Event()
    child_entered = asyncio.Event()
    blocker_started = asyncio.Event()
    blocker_release = asyncio.Event()

    async def stale_child() -> list[str]:
        await child_release.wait()

        async def enter() -> str:
            child_entered.set()
            return "child"

        return await ctx.parallel([enter])

    async def create_stale_child() -> asyncio.Task[list[str]]:
        return asyncio.create_task(stale_child())

    async def blocker() -> str:
        blocker_started.set()
        await blocker_release.wait()
        return "blocker"

    ctx = WorkflowContext(
        FakeFactory([]),
        task_concurrency=1,
    )
    [child_task] = await ctx.parallel([create_stale_child])
    blocker_task = asyncio.create_task(ctx.parallel([blocker]))
    await asyncio.wait_for(blocker_started.wait(), timeout=0.5)

    child_release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert child_entered.is_set() is False

    blocker_release.set()
    assert await blocker_task == ["blocker"]
    assert await asyncio.wait_for(child_task, timeout=0.5) == ["child"]


@pytest.mark.asyncio
async def test_parallel_nested_agent_reuses_single_concurrency_permit():
    ctx = WorkflowContext(
        FakeFactory([FakeSession(reply="nested")]),
        max_concurrency=1,
        task_concurrency=1,
    )

    result = await asyncio.wait_for(
        ctx.parallel([lambda: ctx.agent("inside thunk")]),
        timeout=0.5,
    )

    assert result == ["nested"]


@pytest.mark.asyncio
async def test_parallel_thunk_can_gather_agents_at_single_concurrency():
    ctx = WorkflowContext(
        FakeFactory(
            [
                FakeSession(reply="first"),
                FakeSession(reply="second"),
            ]
        ),
        max_concurrency=1,
        task_concurrency=1,
    )

    async def gather_agents():
        return await asyncio.gather(
            ctx.agent("first nested agent"),
            ctx.agent("second nested agent"),
        )

    result = await asyncio.wait_for(
        ctx.parallel([gather_agents]),
        timeout=0.5,
    )

    assert result == [["first", "second"]]


@pytest.mark.asyncio
async def test_parallel_task_creation_is_bounded_by_concurrency():
    release = asyncio.Event()
    admitted = asyncio.Event()
    running = 0

    async def blocked(index: int) -> int:
        nonlocal running
        running += 1
        if running == 3:
            admitted.set()
        try:
            await release.wait()
            return index
        finally:
            running -= 1

    ctx = WorkflowContext(
        FakeFactory([]),
        max_concurrency=8,
        task_concurrency=3,
    )
    baseline = len(asyncio.all_tasks())
    task = asyncio.create_task(
        ctx.parallel([lambda i=i: blocked(i) for i in range(2_000)])
    )
    try:
        await asyncio.wait_for(admitted.wait(), timeout=0.5)
        # The caller plus a fixed worker set may exist, but there must not be
        # one live asyncio Task per queued thunk.
        assert len(asyncio.all_tasks()) - baseline <= 5
    finally:
        release.set()

    assert await task == list(range(2_000))


@pytest.mark.asyncio
async def test_draft_findings_uses_the_shared_concurrency_cap():
    first_gate = asyncio.Event()
    second_gate = asyncio.Event()
    factory = FakeFactory(
        [FakeSession(gate=first_gate), FakeSession(gate=second_gate)]
    )
    ctx = WorkflowContext(factory, max_concurrency=1)

    first = asyncio.create_task(ctx.draft_findings("first"))
    for _ in range(10):
        await asyncio.sleep(0)
        if factory.builds:
            break
    second = asyncio.create_task(ctx.draft_findings("second"))
    for _ in range(10):
        await asyncio.sleep(0)

    assert len(factory.builds) == 1
    first_gate.set()
    assert await first is None
    for _ in range(10):
        await asyncio.sleep(0)
        if len(factory.builds) == 2:
            break
    assert len(factory.builds) == 2
    second_gate.set()
    assert await second is None

@pytest.mark.asyncio
async def test_parallel_thunk_exception_becomes_none():
    ok = FakeSession(reply="ok")
    boom = FakeSession(boom=True)
    factory = FakeFactory([ok, boom])
    ctx = WorkflowContext(factory)

    async def raising() -> str:
        raise ValueError("thunk blew up")

    results = await ctx.parallel(
        [
            lambda: ctx.agent("a"),
            raising,
            lambda: ctx.agent("b"),
        ]
    )

    # ok agent -> "ok", raising thunk -> None, boom agent -> None
    assert results == ["ok", None, None]

@pytest.mark.asyncio
async def test_parallel_empty():
    ctx = WorkflowContext(FakeFactory([]))
    assert await ctx.parallel([]) == []

@pytest.mark.asyncio
async def test_pipeline_no_inter_stage_barrier():
    """Item B reaches stage 1 while item A is already past stage 2.

    Item A's stage 1 is fast; item B's stage 1 is gated. We prove A completes
    all stages before B even leaves stage 1 — impossible if a barrier existed
    between stages.
    """
    order: list[str] = []
    b_stage1_gate = asyncio.Event()

    async def stage1(prev: Any, item: str, idx: int) -> str:
        order.append(f"{item}-s1-enter")
        if item == "B":
            await b_stage1_gate.wait()
        order.append(f"{item}-s1-exit")
        return f"{item}1"

    async def stage2(prev: Any, item: str, idx: int) -> str:
        order.append(f"{item}-s2")
        return f"{prev}2"

    ctx = WorkflowContext(FakeFactory([]))

    async def drive() -> list:
        return await ctx.pipeline(["A", "B"], stage1, stage2)

    task = asyncio.create_task(drive())
    # Let A flow through both stages while B is stuck in stage 1.
    for _ in range(20):
        await asyncio.sleep(0)
    assert "A-s2" in order  # A is past stage 2
    assert "B-s1-exit" not in order  # B still inside stage 1 -> no barrier

    b_stage1_gate.set()
    results = await task
    assert results == ["A12", "B12"]

@pytest.mark.asyncio
async def test_pipeline_stage_exception_drops_item_and_skips_rest():
    seen_stage2: list[str] = []

    async def stage1(prev: Any, item: str, idx: int) -> str:
        if item == "bad":
            raise RuntimeError("stage 1 failed")
        return f"{item}!"

    async def stage2(prev: Any, item: str, idx: int) -> str:
        seen_stage2.append(item)
        return f"{prev}?"

    ctx = WorkflowContext(FakeFactory([]))
    results = await ctx.pipeline(["good", "bad", "fine"], stage1, stage2)

    assert results == ["good!?", None, "fine!?"]
    # The failed item never reaches stage 2.
    assert "bad" not in seen_stage2
    assert sorted(seen_stage2) == ["fine", "good"]


@pytest.mark.asyncio
async def test_pipeline_stops_after_agent_failure_returns_none():
    ctx = WorkflowContext(FakeFactory([FakeSession(boom=True)]))
    later_inputs: list[Any] = []

    async def agent_stage(_previous: Any, _item: str, _idx: int) -> Any:
        return await ctx.agent("fail")

    async def later_stage(previous: Any, _item: str, _idx: int) -> str:
        later_inputs.append(previous)
        return "unexpected"

    assert await ctx.pipeline(["item"], agent_stage, later_stage) == [None]
    assert later_inputs == []


@pytest.mark.asyncio
async def test_pipeline_can_explicitly_continue_after_business_none():
    seen: list[Any] = []

    async def returns_none(_previous: Any, _item: str, _idx: int) -> None:
        return None

    async def consume_none(previous: Any, _item: str, _idx: int) -> str:
        seen.append(previous)
        return "continued"

    ctx = WorkflowContext(FakeFactory([]))
    result = await ctx.pipeline(
        ["item"],
        returns_none,
        consume_none,
        stop_on_none=False,
    )

    assert result == ["continued"]
    assert seen == [None]


@pytest.mark.asyncio
async def test_pipeline_passes_index_and_original_item():
    captured: list[tuple[Any, str, int]] = []

    async def stage(prev: Any, item: str, idx: int) -> str:
        captured.append((prev, item, idx))
        return item.upper()

    ctx = WorkflowContext(FakeFactory([]))
    results = await ctx.pipeline(["x", "y"], stage)

    assert results == ["X", "Y"]
    assert captured == [("x", "x", 0), ("y", "y", 1)]

@pytest.mark.asyncio
async def test_budget_spent_sums_session_tokens():
    s1 = FakeSession(reply="a", tokens=100)
    s2 = FakeSession(reply="b", tokens=250)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=10_000)

    assert ctx.budget.spent() == 0
    await ctx.agent("one")
    assert ctx.budget.spent() == 100
    await ctx.agent("two")
    assert ctx.budget.spent() == 350
    assert ctx.budget.remaining() == 10_000 - 350

@pytest.mark.asyncio
async def test_budget_exceeded_raises_before_next_call():
    s1 = FakeSession(reply="a", tokens=500)
    s2 = FakeSession(reply="b", tokens=0)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=500)

    assert await ctx.agent("first") == "a"  # spends 500, reaching the cap
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.agent("second")

@pytest.mark.asyncio
async def test_over_budget_ok_bypasses_the_pre_call_raise():
    """``over_budget_ok=True`` lets the budget-floor's forced write run past zero.

    The single guaranteed final write must execute even with the meter already
    exhausted — otherwise it self-aborts on the pre-call gate and no patch lands
    (the sympy-11400 regression). It is bounded instead by ``thinking=False`` +
    a wall-clock ``timeout``, not by this budget gate. The default path stays
    gated. The pre-call raise fires before any session is consumed, so the gated
    attempt does not eat ``s2``.
    """
    s1 = FakeSession(reply="a", tokens=500)
    s2 = FakeSession(reply="forced", tokens=0)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=500)

    assert await ctx.agent("first") == "a"  # spends 500, exhausting the budget
    assert ctx.budget.remaining() <= 0
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.agent("default is still gated")
    assert await ctx.agent("forced write", over_budget_ok=True) == "forced"

@pytest.mark.asyncio
async def test_budget_none_never_raises():
    s1 = FakeSession(reply="a", tokens=10_000_000)
    s2 = FakeSession(reply="b", tokens=10_000_000)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=None)

    assert await ctx.agent("a") == "a"
    assert await ctx.agent("b") == "b"
    assert ctx.budget.total is None
    assert ctx.budget.remaining() == float("inf")

@pytest.mark.asyncio
async def test_parallel_propagates_budget_exceeded():
    """A parallel fan-out must preserve the workflow terminal budget signal."""
    s1 = FakeSession(reply="a", tokens=500)
    s2 = FakeSession(reply="b", tokens=0)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=500)

    # First call spends the whole budget; the second starts already exhausted.
    assert await ctx.agent("warm up") == "a"

    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.parallel([lambda: ctx.agent("exhausted")])


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ("parallel", "pipeline"))
async def test_collections_settle_gated_siblings_before_propagating_budget_stop(
    composition,
):
    sibling_started = asyncio.Event()
    budget_ready = asyncio.Event()
    release_sibling = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def gated_sibling():
        sibling_started.set()
        try:
            await release_sibling.wait()
            return "finished"
        finally:
            sibling_finished.set()

    async def exhausted():
        await sibling_started.wait()
        budget_ready.set()
        raise WorkflowBudgetExceeded("budget exhausted")

    ctx = WorkflowContext(FakeFactory([]))
    if composition == "parallel":
        task = asyncio.create_task(ctx.parallel([exhausted, gated_sibling]))
    else:

        async def stage(_previous, _item, index):
            if index == 0:
                return await exhausted()
            return await gated_sibling()

        task = asyncio.create_task(ctx.pipeline(["exhausted", "gated"], stage))

    try:
        await budget_ready.wait()
        for _ in range(20):
            await asyncio.sleep(0)
            if task.done():
                break
        assert task.done() is False
        release_sibling.set()
        with pytest.raises(WorkflowBudgetExceeded):
            await task
        assert sibling_finished.is_set()
    finally:
        release_sibling.set()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_pipeline_propagates_budget_exceeded_and_skips_later_stages():
    """A pipeline must preserve the workflow terminal budget signal."""
    s1 = FakeSession(reply="a", tokens=500)
    s2 = FakeSession(reply="b", tokens=0)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=500)

    # Spend the whole budget so the pipeline's agent stage starts exhausted.
    assert await ctx.agent("warm up") == "a"

    later_ran: list[int] = []

    async def agent_stage(prev: Any, item: int, idx: int) -> Any:
        return await ctx.agent(f"item {item}")

    async def later_stage(prev: Any, item: int, idx: int) -> Any:
        later_ran.append(item)
        return prev

    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.pipeline([7], agent_stage, later_stage)
    # The exhausted item never reaches the later stage.
    assert later_ran == []
