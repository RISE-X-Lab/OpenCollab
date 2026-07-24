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
    ctx = WorkflowContext(factory, max_concurrency=2)

    thunks = [(lambda i=i: ctx.agent(f"p{i}")) for i in range(n)]
    results = await ctx.parallel(thunks)

    assert sorted(results) == [str(i) for i in range(n)]
    assert high_water <= 2

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
async def test_parallel_swallows_budget_exceeded_to_none():
    """A budget-exhausted ctx.agent() inside a parallel thunk resolves to None.

    WorkflowBudgetExceeded escapes ctx.agent() at the WorkflowContext level, but
    parallel()'s per-slot guard localizes ANY exception (including the budget
    one) to that slot — it must not abort the gather.
    """
    s1 = FakeSession(reply="a", tokens=500)
    s2 = FakeSession(reply="b", tokens=0)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=500)

    # First call spends the whole budget; the second starts already exhausted.
    assert await ctx.agent("warm up") == "a"

    results = await ctx.parallel([lambda: ctx.agent("exhausted")])

    assert results == [None]

@pytest.mark.asyncio
async def test_pipeline_swallows_budget_exceeded_to_none_and_skips_rest():
    """A budget-exhausted ctx.agent() in a pipeline stage drops the item to None.

    The exhausted stage raises WorkflowBudgetExceeded; pipeline()'s flow guard
    drops that item to None and skips its remaining stages, leaving other items
    untouched.
    """
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

    results = await ctx.pipeline([7], agent_stage, later_stage)

    assert results == [None]
    # The exhausted item never reaches the later stage.
    assert later_ran == []
