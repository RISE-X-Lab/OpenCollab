"""Tests for the WorkflowContext mini workflow engine core (phase 1).

A fake session factory provides scripted sessions (no LLM). The fakes record
the prompt they were seeded with, can simulate work taking time, and report a
fixed ``used_tokens`` so budget accounting can be asserted deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from opencollab.application.workflow import (
    WorkflowBudgetExceeded,
    WorkflowContext,
)


class FakeState:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []


class FakeSession:
    """A scripted one-shot session.

    ``reply`` is the run_loop() return value. ``tokens`` is reported via
    ``used_tokens``. ``gate`` (optional) lets a test hold run_loop() open to
    observe concurrency; ``started`` is set when run_loop() begins. ``boom``
    makes run_loop() raise to exercise error capture.
    """

    def __init__(
        self,
        *,
        reply: str = "done",
        tokens: int = 0,
        gate: asyncio.Event | None = None,
        boom: bool = False,
        on_enter: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.reply = reply
        self._tokens = tokens
        self._gate = gate
        self._boom = boom
        self._on_enter = on_enter
        self.state = FakeState()
        self.prompt: str | None = None

    async def add_user_message(self, content: str) -> None:
        self.prompt = content
        self.state.messages.append({"role": "user", "content": content})

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        if self._on_enter is not None:
            await self._on_enter()
        if self._gate is not None:
            await self._gate.wait()
        if self._boom:
            raise RuntimeError("agent exploded")
        return self.reply

    @property
    def used_tokens(self) -> int:
        return self._tokens


class FakeFactory:
    """Hands out pre-scripted sessions in build order, recording build calls."""

    def __init__(self, sessions: Sequence[FakeSession]) -> None:
        self._sessions = list(sessions)
        self._idx = 0
        self.builds: list[dict[str, Any]] = []

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
    ) -> FakeSession:
        self.builds.append(
            {
                "prompt": prompt,
                "budget": budget,
                "tools": tools,
                "isolation": isolation,
                "label": label,
            }
        )
        session = self._sessions[self._idx]
        self._idx += 1
        return session


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


# --------------------------------------------------------------------------- #
# agent()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_returns_final_text_and_seeds_prompt():
    session = FakeSession(reply="the answer")
    factory = FakeFactory([session])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("solve it")

    assert result == "the answer"
    assert session.prompt == "solve it"
    assert factory.builds[0]["prompt"] == "solve it"


@pytest.mark.asyncio
async def test_agent_error_returns_none():
    session = FakeSession(boom=True)
    ctx = WorkflowContext(FakeFactory([session]))

    assert await ctx.agent("do it") is None


@pytest.mark.asyncio
async def test_agent_forwards_tools_and_isolation():
    session = FakeSession()
    factory = FakeFactory([session])
    ctx = WorkflowContext(factory)

    await ctx.agent("p", tools=["t1"], isolation=True)

    assert factory.builds[0]["tools"] == ["t1"]
    assert factory.builds[0]["isolation"] is True


# --------------------------------------------------------------------------- #
# concurrency cap
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# parallel()
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# pipeline()
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


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
async def test_budget_none_never_raises():
    s1 = FakeSession(reply="a", tokens=10_000_000)
    s2 = FakeSession(reply="b", tokens=10_000_000)
    ctx = WorkflowContext(FakeFactory([s1, s2]), budget_total=None)

    assert await ctx.agent("a") == "a"
    assert await ctx.agent("b") == "b"
    assert ctx.budget.total is None
    assert ctx.budget.remaining() == float("inf")


# --------------------------------------------------------------------------- #
# budget-exceeded swallow contract inside parallel() / pipeline()
# --------------------------------------------------------------------------- #


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


class _DeferredTokenSession(FakeSession):
    """A FakeSession whose reported token spend can be deferred.

    ``used_tokens`` reads 0 until ``land_spend()`` flips it to ``_tokens``. This
    models a concurrent agent whose spend lands AFTER another agent has passed
    agent()'s budget gate but BEFORE that agent computes its per-session budget —
    the exact window in which a naive ``int(remaining)`` would go negative.
    """

    def __init__(self, *, tokens: int, on_enter=None) -> None:
        super().__init__(reply="a", tokens=tokens, on_enter=on_enter)
        self._landed = False

    def land_spend(self) -> None:
        self._landed = True

    @property
    def used_tokens(self) -> int:
        return self._tokens if self._landed else 0


@pytest.mark.asyncio
async def test_session_budget_clamped_to_zero_under_concurrent_overspend():
    """_session_budget() never hands a negative budget to a concurrent build.

    Total budget is 100. The first agent holds the single semaphore permit and
    parks in run_loop; its 150-token spend has not landed yet (spent==0). The
    second agent then passes agent()'s budget gate (spent==0) and BLOCKS on the
    semaphore — it is now between its gate check and its session build. We land
    the first session's spend (spent jumps to 150, remaining == -50) and release
    the first agent. When the second agent finally builds, ``_session_budget()``
    must clamp its per-session budget to 0, never -50.

    Deterministic via asyncio.Event handoff + ``asyncio.sleep(0)`` yields only;
    no real-duration sleeps. ``max_concurrency=1`` makes the semaphore serialize
    the two agents so the blocking window is guaranteed.
    """
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

    # 2. Second agent: passes gate (spent still 0), then blocks on the permit.
    second_task = asyncio.create_task(ctx.agent("second"))
    for _ in range(5):  # let it clear the gate and park on the semaphore
        await asyncio.sleep(0)
    assert len(factory.builds) == 1  # it has NOT built yet (still gated by permit)

    # 3. Land the first session's spend: spent jumps to 150, remaining == -50.
    s1.land_spend()
    assert ctx.budget.spent() == 150

    # 4. Release the first agent; the second now acquires the permit and builds
    #    its session against the overspent budget.
    release_first.set()
    second_result = await asyncio.wait_for(second_task, timeout=1.0)
    assert await first_task == "a"

    assert second_result == "b"
    assert len(factory.builds) == 2
    # The clamp: int(-50) would be negative; the budget must floor at 0.
    assert factory.builds[1]["budget"] == 0


# --------------------------------------------------------------------------- #
# phase() / log()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_phase_and_log_emit_via_sink():
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([]), event_sink=sink)

    await ctx.phase("planning")
    await ctx.log("hello world")

    assert len(sink.events) == 2


@pytest.mark.asyncio
async def test_phase_and_log_noop_without_sink():
    ctx = WorkflowContext(FakeFactory([]))
    # Must not raise when no sink is wired.
    await ctx.phase("planning")
    await ctx.log("hello world")
