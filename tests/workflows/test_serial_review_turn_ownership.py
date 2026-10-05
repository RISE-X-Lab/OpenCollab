"""Review suspension retains exclusive execution and cancellation ownership."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.tools.spawn import SpawnWithReviewTool
from opencollab.application.event_bus import EventBus
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler, terminal


async def _wait_for_queued_turn(scheduler, aid):
    async def wait():
        while aid not in scheduler._turn_waiters:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("outcome", ["return", "exception", "cancel"])
async def test_review_reacquires_before_parent_work_and_cancel_keeps_other_owner(
    outcome, monkeypatch,
):
    other_started = asyncio.Event()
    other_release = asyncio.Event()
    parent_finished = asyncio.Event()
    captured = {}
    work = []

    async def other_turn(session):
        assert work == []
        work.append("tester")
        other_started.set()
        await other_release.wait()
        assert work.pop() == "tester"
        session.state.set_phase(SessionPhase.DONE)
        return "other complete"

    async def reviewer_turn(session):
        assert work == []
        aid = await session.scheduler.spawn(0, "tester", "independent work")
        captured["other_aid"] = aid
        # The tester queues behind this reviewer, ahead of the waiting parent.
        await _wait_for_queued_turn(session.scheduler, aid)
        session.state.set_phase(SessionPhase.DONE)
        return "VERDICT: PASS"

    async def parent_turn(session):
        assert work == []
        work.append("lead")
        session.state.set_phase(SessionPhase.EXECUTING_TOOLS)
        tool = SpawnWithReviewTool(session.scheduler)
        execute = tool.execute_with_runtime

        async def capture_execution(params, runtime):
            captured["tool_task"] = asyncio.current_task()
            return await execute(params, runtime)

        tool.execute_with_runtime = capture_execution
        executor = ToolExecutionUseCase(
            agent=Agent(name="lead", system_prompt="lead", tools=[tool]),
            environment=None, state=session.state, event_publisher=EventBus(None),
        )
        work.pop()
        result, _latency = await executor.execute_tool(tool, {"task": "implement", "max_iterations": 1})
        assert work == []
        work.append("lead")
        captured["result"] = result
        parent_finished.set()
        work.pop()
        session.state.set_phase(SessionPhase.DONE)
        session.state.append_message({"role": "assistant", "content": result})
        return result

    lead = ScriptedSession("lead", [parent_turn, terminal("recovered")])
    coder = ScriptedSession("coder", [terminal("implementation")])
    reviewer = ScriptedSession("reviewer", [reviewer_turn])
    tester = ScriptedSession("tester", [other_turn])
    scheduler, _events = build_scheduler(lead, [coder, reviewer, tester], serialize_turns=True)

    if outcome == "exception":
        async def fail_review(*_args):
            aid = await scheduler.spawn(0, "coder", "prepare")
            await scheduler.wait_until_terminal(aid)
            aid = await scheduler.spawn(0, "reviewer", "prepare review")
            await scheduler.wait_until_terminal(aid)
            raise ValueError("controlled review failure")

        monkeypatch.setattr("opencollab.application._scheduler_review.run_spawn_with_review", fail_review)

    driver = scheduler._start_agent_task(0, lead)
    try:
        await asyncio.wait_for(other_started.wait(), 5)
        await _wait_for_queued_turn(scheduler, 0)
        assert not parent_finished.is_set()
        assert scheduler._turn_gate_lock.locked()
        if outcome == "cancel":
            captured["tool_task"].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(driver, 5)
            assert scheduler._turn_gate_lock.locked()
            assert work == ["tester"]
            assert not scheduler._tasks[captured["other_aid"]].done()
        other_release.set()
        if outcome != "cancel":
            await asyncio.wait_for(driver, 5)
            assert parent_finished.is_set()
            expected = "PASSED" if outcome == "return" else "Tool execution error: ValueError"
            assert expected in captured["result"]
        await scheduler.wait_until_terminal(captured["other_aid"])
        assert not scheduler._turn_gate_lock.locked()
        assert not scheduler._turn_waiters
        assert await asyncio.wait_for(scheduler.run("continue"), 5) == "recovered"
    finally:
        other_release.set()
        await scheduler.cleanup(cleanup_timeout=1)


async def test_cancelled_review_wait_releases_only_parent_and_later_turn_runs():
    child_started = asyncio.Event()
    child_release = asyncio.Event()
    captured = {}

    async def coder_turn(session):
        child_started.set()
        await child_release.wait()
        session.state.set_phase(SessionPhase.DONE)
        return "implementation"

    async def parent_turn(session):
        session.state.set_phase(SessionPhase.EXECUTING_TOOLS)
        captured["tool_task"] = asyncio.create_task(
            session.scheduler.spawn_with_review(0, "implement", max_iterations=1)
        )
        return await captured["tool_task"]

    lead = ScriptedSession("lead", [parent_turn, terminal("recovered")])
    coder = ScriptedSession("coder", [coder_turn])
    scheduler, _events = build_scheduler(lead, [coder], serialize_turns=True)
    driver = scheduler._start_agent_task(0, lead)
    try:
        await asyncio.wait_for(child_started.wait(), 5)
        captured["tool_task"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(driver, 5)
        # A review wait shields its child; cancelling the parent cannot release
        # the lock the still-running coder now owns.
        assert scheduler._turn_gate_lock.locked()
        child_release.set()
        await scheduler.wait_until_terminal(1)
        assert not scheduler._turn_gate_lock.locked()
        assert await asyncio.wait_for(scheduler.run("continue"), 5) == "recovered"
    finally:
        child_release.set()
        await scheduler.cleanup(cleanup_timeout=1)


async def test_external_review_waits_for_current_driver_to_finish():
    lead_started = asyncio.Event()
    lead_release = asyncio.Event()
    coder_started = asyncio.Event()

    async def lead_turn(session):
        lead_started.set()
        await lead_release.wait()
        session.state.set_phase(SessionPhase.DONE)
        return "lead complete"

    async def coder_turn(session):
        coder_started.set()
        session.state.set_phase(SessionPhase.DONE)
        return "implementation"

    lead = ScriptedSession("lead", [lead_turn])
    coder = ScriptedSession("coder", [coder_turn])
    reviewer = ScriptedSession("reviewer", [terminal("VERDICT: PASS")])
    scheduler, _events = build_scheduler(lead, [coder, reviewer], serialize_turns=True)
    driver = scheduler._start_agent_task(0, lead)
    review = None
    try:
        await asyncio.wait_for(lead_started.wait(), 5)
        review = asyncio.create_task(scheduler.spawn_with_review(0, "implement", max_iterations=1))
        await _wait_for_queued_turn(scheduler, 1)
        assert not coder_started.is_set()
        assert scheduler._turn_gate_lock.locked()
        lead_release.set()
        await asyncio.wait_for(driver, 5)
        assert "PASSED after 1 iteration" in await asyncio.wait_for(review, 5)
        assert coder_started.is_set()
        assert not scheduler._turn_gate_lock.locked()
    finally:
        lead_release.set()
        if review is not None and not review.done():
            review.cancel()
            await asyncio.gather(review, return_exceptions=True)
        await scheduler.cleanup(cleanup_timeout=1)
