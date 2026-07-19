"""Tests for the WorkflowContext mini workflow engine core (phase 1).

A fake session factory provides scripted sessions (no LLM). The fakes record
the prompt they were seeded with, can simulate work taking time, and report a
fixed ``used_tokens`` so budget accounting can be asserted deterministically.
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

import opencollab.application.workflow as workflow_module
from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.application.workflow import (
    WorkflowBudgetExceeded,
    WorkflowContext,
)
from opencollab.domain.session import TurnEnforcementState


class FakeState:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.turn = TurnEnforcementState()


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


class CancelCleanupSession(FakeSession):
    def __init__(self, *, tokens_after_cancel: int = 0) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()
        self.started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self._tokens_after_cancel = tokens_after_cancel
        self._landed = False

    @property
    def used_tokens(self) -> int:
        return self._tokens_after_cancel if self._landed else 0

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        gate = asyncio.Event()
        self.started.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_cancel.wait()
            self._landed = True
            raise


class StubbornAddSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.add_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_add = asyncio.Event()
        self.run_loop_called = False

    async def add_user_message(self, content: str) -> None:
        self.add_started.set()
        while not self.release_add.is_set():
            try:
                await self.release_add.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
        await super().add_user_message(content)

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        self.run_loop_called = True
        return await super().run_loop(cancel_event)


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
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> FakeSession:
        self.builds.append(
            {
                "prompt": prompt,
                "budget": budget,
                "tools": tools,
                "isolation": isolation,
                "label": label,
                "tool_choice": tool_choice,
                "thinking": thinking,
            }
        )
        session = self._sessions[self._idx]
        self._idx += 1
        return session


@pytest.mark.parametrize("max_concurrency", [0, -1, 1.5, True, "2", float("nan")])
def test_workflow_context_rejects_invalid_concurrency(max_concurrency):
    with pytest.raises(ValueError, match="max_concurrency must be a positive integer"):
        WorkflowContext(FakeFactory([]), max_concurrency=max_concurrency)


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


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True, "bad"])
@pytest.mark.asyncio
async def test_agent_rejects_invalid_timeout_before_building_session(timeout):
    factory = FakeFactory([])
    ctx = WorkflowContext(factory)

    with pytest.raises(ValueError, match="workflow timeout"):
        await ctx.agent("must not start", timeout=timeout)

    assert factory.builds == []


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
async def test_uncapped_parallel_agents_split_the_available_budget_fairly():
    release = asyncio.Event()
    sessions = [FakeSession(reply=str(i), gate=release) for i in range(3)]
    factory = FakeFactory(sessions)
    ctx = WorkflowContext(factory, budget_total=90, max_concurrency=3)

    task = asyncio.create_task(
        ctx.parallel([lambda i=i: ctx.agent(f"agent {i}") for i in range(3)])
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if len(factory.builds) == 3:
            break

    grants = [build["budget"] for build in factory.builds]
    assert grants == [30, 30, 30]
    assert sum(grants) <= 90
    release.set()
    assert await task == ["0", "1", "2"]


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
    await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=0.5)

    second_task = asyncio.create_task(ctx.agent("second"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(factory.builds) == 1
    assert second_task.done() is False

    timed_out.release_cancel.set()
    assert await asyncio.wait_for(second_task, timeout=0.5) == "second"
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

    await asyncio.wait_for(started.wait(), timeout=0.5)
    assert agent_task in ctx.pending_cleanup_tasks

    release.set()
    assert await asyncio.wait_for(agent_task, timeout=0.5) == "done"
    await asyncio.sleep(0)
    assert ctx.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_pending_cleanup_callback_consumes_late_exception():
    class FailingCancelCleanupSession(CancelCleanupSession):
        async def run_loop(self, cancel_event=None):
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
        assert await ctx.agent("slow", timeout=0.05) is None
        await asyncio.wait_for(session.cancel_seen.wait(), timeout=0.5)
        session.release_cancel.set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)
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
    await asyncio.wait_for(timed_out.cancel_seen.wait(), timeout=0.5)
    timed_out.release_cancel.set()
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)


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
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    await ctx._budget_lock.acquire()
    try:
        release_first.set()
        for _ in range(5):
            await asyncio.sleep(0)
        first_task.cancel()
        assert await asyncio.wait_for(first_task, timeout=0.5) == "first"
        assert ctx.budget._leases == []
    finally:
        ctx._budget_lock.release()

    assert await asyncio.wait_for(
        ctx.agent("second", budget=80),
        timeout=0.5,
    ) == "second"
    assert ctx.budget._leases == []


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


@pytest.mark.asyncio
async def test_phase_and_log_ignore_observer_failures():
    class FailingSink:
        async def emit(self, event: Any) -> None:
            raise RuntimeError("sink failed")

    class FailingTracer:
        def log_step(self, **kwargs: Any) -> None:
            raise RuntimeError("trace failed")

    ctx = WorkflowContext(
        FakeFactory([]), event_sink=FailingSink(), tracer=FailingTracer()
    )

    await ctx.phase("planning")
    await ctx.log("still running")


# --------------------------------------------------------------------------- #
# working-tree probe (P0-1)
# --------------------------------------------------------------------------- #


class FakeProbe:
    """A scripted WorkingTreeProbe recording how often it is asked.

    ``changed`` is the whole-tree answer. ``changed_excluding`` honors a
    ``{path: dirty}`` map when given (a path present in ``excludes`` whose only
    dirt is itself drops out): with no map it falls back to a scripted
    ``excluded_changed`` bool so tests can assert "tree dirty but source clean".
    """

    def __init__(
        self,
        *,
        changed: bool = True,
        boom: bool = False,
        excluded_changed: bool | None = None,
    ) -> None:
        self._changed = changed
        self._boom = boom
        self._excluded_changed = excluded_changed
        self.calls = 0
        self.exclude_calls: list[tuple[str, ...]] = []

    async def changed(self) -> bool:
        self.calls += 1
        if self._boom:
            raise RuntimeError("git unavailable")
        return self._changed

    async def changed_excluding(self, paths) -> bool:
        self.exclude_calls.append(tuple(paths))
        if self._boom:
            raise RuntimeError("git unavailable")
        if not paths:
            return self._changed
        if self._excluded_changed is not None:
            return self._excluded_changed
        return self._changed

    async def diff(self) -> str:
        return "diff"


@pytest.mark.asyncio
async def test_tree_changed_is_none_without_probe():
    # No probe wired -> "cannot verify" -> None (callers must not hard-block).
    ctx = WorkflowContext(FakeFactory([]))
    assert await ctx.tree_changed() is None


@pytest.mark.asyncio
async def test_tree_changed_proxies_probe_result():
    ctx_yes = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(changed=True))
    ctx_no = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(changed=False))
    assert await ctx_yes.tree_changed() is True
    assert await ctx_no.tree_changed() is False


@pytest.mark.asyncio
async def test_tree_changed_swallows_probe_error_to_none():
    # A flaky git call must never abort the run: error -> None.
    ctx = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(boom=True))
    assert await ctx.tree_changed() is None


# --------------------------------------------------------------------------- #
# source-scoped probe (Bug A): excludes harness-injected test paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_source_changed_excludes_injected_paths():
    # The whole tree is dirty (changed=True) only because the harness git-applied
    # an injected test; with that path excluded the SOURCE is clean -> False, while
    # tree_changed still reports True. This is the core of Bug A.
    probe = FakeProbe(changed=True, excluded_changed=False)
    ctx = WorkflowContext(FakeFactory([]), tree_probe=probe)

    assert await ctx.tree_changed() is True
    assert await ctx.source_changed(["t/test_x.py"]) is False
    assert probe.exclude_calls == [("t/test_x.py",)]


@pytest.mark.asyncio
async def test_source_changed_is_none_without_probe():
    # No probe wired -> "cannot verify" -> None (callers must not hard-block).
    ctx = WorkflowContext(FakeFactory([]))
    assert await ctx.source_changed(["t/test_x.py"]) is None


@pytest.mark.asyncio
async def test_source_changed_swallows_probe_error_to_none():
    # A flaky git call must never abort the run: probe error -> None.
    ctx = WorkflowContext(FakeFactory([]), tree_probe=FakeProbe(boom=True))
    assert await ctx.source_changed(["t/test_x.py"]) is None


@pytest.mark.asyncio
async def test_agent_threads_tool_choice_to_factory():
    factory = FakeFactory([FakeSession(reply="ok")])
    ctx = WorkflowContext(factory)
    # Ordinary call: no tool_choice forced.
    await ctx.agent("normal")
    assert factory.builds[-1]["tool_choice"] is None
    factory._sessions.append(FakeSession(reply="ok"))
    # Forced call: tool_choice="required" reaches the factory build.
    await ctx.agent("forced", tool_choice="required")
    assert factory.builds[-1]["tool_choice"] == "required"


# --------------------------------------------------------------------------- #
# free-text path: thinking override + per-call timeout clamp (P7 timing gap)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_threads_thinking_to_factory_on_free_text_path():
    factory = FakeFactory([FakeSession(reply="ok"), FakeSession(reply="ok")])
    ctx = WorkflowContext(factory)
    # Default: thinking left None so the factory's run-wide default applies.
    await ctx.agent("normal")
    assert factory.builds[-1]["thinking"] is None
    # Forced write: thinking=False reaches the factory build (fast generation).
    await ctx.agent("forced", thinking=False)
    assert factory.builds[-1]["thinking"] is False


@pytest.mark.asyncio
async def test_agent_timeout_bounds_run_loop_and_returns_none():
    # A run_loop held open past the timeout is cancelled by asyncio.wait_for; the
    # call returns None (one dead agent never kills the fleet) and logs the timeout.
    gate = asyncio.Event()  # never set -> run_loop would block forever
    session = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await ctx.agent("slow", timeout=0.01)

    assert result is None
    assert any("timed out" in e.message for e in sink.events)


@pytest.mark.asyncio
async def test_agent_timeout_owns_stubborn_initial_message_task():
    session = StubbornAddSession()
    ctx = WorkflowContext(FakeFactory([session]))

    try:
        result = await asyncio.wait_for(
            ctx.agent("slow add", timeout=0.01),
            timeout=0.5,
        )

        assert result is None
        await asyncio.wait_for(session.cancel_seen.wait(), timeout=0.5)
        assert session.run_loop_called is False
        assert ctx.pending_cleanup_tasks
    finally:
        session.release_add.set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)


@pytest.mark.asyncio
async def test_cancelled_draft_keeps_lease_until_stubborn_message_finishes():
    stubborn = StubbornAddSession()
    second_gate = asyncio.Event()
    second = FakeSession(reply="second", gate=second_gate)
    factory = FakeFactory([stubborn, second])
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=2)

    draft_task = asyncio.create_task(
        ctx.draft_findings("draft", budget=80)
    )
    await asyncio.wait_for(stubborn.add_started.wait(), timeout=0.5)
    draft_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(draft_task, timeout=0.5)
    await asyncio.wait_for(stubborn.cancel_seen.wait(), timeout=0.5)

    second_task = asyncio.create_task(ctx.agent("second", budget=80))
    for _ in range(20):
        await asyncio.sleep(0)
        if len(factory.builds) == 2:
            break

    assert factory.builds[1]["budget"] == 20
    stubborn.release_add.set()
    second_gate.set()
    assert await second_task == "second"
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)


@pytest.mark.asyncio
async def test_draft_findings_has_internal_wall_timeout(monkeypatch):
    gate = asyncio.Event()
    session = FakeSession(reply="never", gate=gate)
    ctx = WorkflowContext(FakeFactory([session]))
    monkeypatch.setattr(
        workflow_module,
        "DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS",
        0.01,
    )

    result = await asyncio.wait_for(ctx.draft_findings("draft"), timeout=0.5)

    assert result is None
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)


@pytest.mark.asyncio
async def test_structured_agent_timeout_bounds_first_pass_and_returns_none():
    gate = asyncio.Event()
    session = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await ctx.agent(
        "slow structured",
        schema={
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string"}},
        },
        timeout=0.01,
    )

    assert result is None
    assert any("structured agent timed out" in e.message for e in sink.events)


@pytest.mark.asyncio
async def test_structured_agent_timeout_bounds_forced_retry_and_returns_none():
    first = FakeSession(reply="prose instead of structured output")
    gate = asyncio.Event()
    retry = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([first, retry]), event_sink=sink)

    result = await ctx.agent(
        "needs structured",
        schema={
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string"}},
        },
        timeout=0.01,
    )

    assert result is None
    assert any("structured retry timed out" in e.message for e in sink.events)


@pytest.mark.asyncio
async def test_structured_agent_timeout_returns_before_cancel_cleanup_finishes():
    session = CancelCleanupSession()
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await asyncio.wait_for(
        ctx.agent(
            "slow structured",
            schema={
                "type": "object",
                "required": ["verdict"],
                "properties": {"verdict": {"type": "string"}},
            },
            timeout=0.01,
        ),
        timeout=0.5,
    )

    assert result is None
    assert any("structured agent timed out" in e.message for e in sink.events)
    session.release_cancel.set()
    await asyncio.sleep(0)
    assert session.cancel_seen.is_set()


@pytest.mark.asyncio
async def test_structured_provider_timeout_is_reported_as_failure_not_caller_deadline():
    class ProviderTimeoutSession(FakeSession):
        async def run_loop(self, cancel_event=None):
            raise asyncio.TimeoutError("provider transport timed out")

    sink = RecordingSink()
    ctx = WorkflowContext(
        FakeFactory([ProviderTimeoutSession()]),
        event_sink=sink,
    )

    result = await ctx.agent(
        "provider timeout",
        schema={"type": "object", "properties": {}},
        timeout=10.0,
    )

    assert result is None
    messages = [event.message for event in sink.events]
    assert any("structured agent failed" in message for message in messages)
    assert not any("structured agent timed out" in message for message in messages)


@pytest.mark.asyncio
async def test_structured_retry_keeps_caller_budget_cap():
    first = FakeSession(reply="prose", tokens=50)
    retry = FakeSession(reply="still prose", tokens=0)
    factory = FakeFactory([first, retry])
    ctx = WorkflowContext(factory, budget_total=1_000)

    await ctx.agent(
        "structured",
        schema={"type": "object", "properties": {}},
        budget=123,
    )

    assert [build["budget"] for build in factory.builds] == [123, 73]


@pytest.mark.asyncio
async def test_agent_infinite_timeout_does_not_bound_run_loop():
    # An infinite timeout (the unbounded-deadline default from seconds_left) must
    # not wrap the loop in wait_for — the call completes normally.
    session = FakeSession(reply="ok")
    ctx = WorkflowContext(FakeFactory([session]))

    result = await ctx.agent("normal", timeout=float("inf"))

    assert result == "ok"
