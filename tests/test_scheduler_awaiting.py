"""Scheduler suspend, resume, messaging, and cancellation tests."""

from __future__ import annotations

import asyncio

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    event_types,
    resume_done,
    run,
    suspend_spawning,
    terminal,
)

from opencollab.domain.pending import PendingRow, PendingRowError, RowKind, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase


def test_single_deferred_spawn_round_trip():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [terminal("child output")])
    scheduler, events = build_scheduler(lead, [child])

    result = run(scheduler.run("please delegate"))

    # The lead reasoned over the child's result in the SAME turn — the gap closed.
    assert result == "final: child output"
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert len(event_types(events, "agent_resumed")) == 1
    assert len(event_types(events, "agent_completed")) == 2  # child + lead

def test_multiple_parallel_children_resume_exactly_once():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning(
                [("coder", "a", "tc-1"), ("coder", "b", "tc-2"), ("coder", "c", "tc-3")]
            ),
            resume_done(lambda results: "merged: " + ",".join(results)),
        ],
    )
    children = [
        ScriptedSession("coder", [terminal("ra")]),
        ScriptedSession("coder", [terminal("rb")]),
        ScriptedSession("coder", [terminal("rc")]),
    ]
    scheduler, events = build_scheduler(lead, children)

    result = run(scheduler.run("delegate three"))

    assert result == "merged: ra,rb,rc"
    # The last child to finish triggers exactly one resume — no double-wake.
    assert len(event_types(events, "agent_resumed")) == 1
    assert lead.state.pending_events.is_empty()


@pytest.mark.parametrize(
    ("phase", "reason", "disposition"),
    [
        (SessionPhase.STOPPED, "budget exceeded", "stopped"),
        (SessionPhase.ERROR, "provider failed", "failed"),
    ],
)
def test_non_done_child_delivers_failed_result_and_event(phase, reason, disposition):
    async def terminal_child(sess: ScriptedSession) -> str:
        sess.state.set_phase(phase)
        sess.state.terminal_reason = reason
        return "partial child output"

    async def resume_after_failed_child(sess: ScriptedSession) -> str:
        row = sess.state.pending_events.rows["tc-1"]
        assert row.status is RowStatus.FAILED
        assert row.result == f"Error: agent {disposition}: {reason}"
        assert row.error == row.result
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "lead recovered"})
        return "lead recovered"

    lead = ScriptedSession(
        "lead",
        [suspend_spawning([("coder", "do it", "tc-1")]), resume_after_failed_child],
    )
    child = ScriptedSession("coder", [terminal_child])
    scheduler, events = build_scheduler(lead, [child])

    assert run(scheduler.run("please delegate")) == "lead recovered"
    assert len(event_types(events, "agent_failed")) == 1
    assert len(event_types(events, "agent_completed")) == 1  # lead only


def test_last_child_completion_in_parent_return_tail_still_resumes():
    lead = ScriptedSession(
        "lead",
        [resume_done(lambda results: f"resumed: {results[0]}")],
    )
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(
            tool_call_id="tail-child",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=1,
        )
    )

    async def scenario():
        release_parent = asyncio.Event()

        async def parent_tail():
            await release_parent.wait()

        original = asyncio.create_task(parent_tail())
        scheduler._tasks[0] = original
        await scheduler._wake(0, "tail-child", "done", RowStatus.DONE)
        assert scheduler._tasks[0] is original
        assert lead.state.pending_events.is_complete()

        release_parent.set()
        await original
        for _ in range(20):
            replacement = scheduler._tasks.get(0)
            if replacement is not None and replacement is not original:
                await replacement
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("parent resume task was never scheduled")

    run(scenario())

    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert scheduler.table.get(0).result == "resumed: done"
    assert len(event_types(events, "agent_resumed")) == 1

def test_nested_delegation_resumes_up_the_tree():
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("manager", "plan", "L1")]),
            resume_done(lambda results: f"lead<{results[0]}>"),
        ],
    )
    manager = ScriptedSession(
        "manager",
        [
            suspend_spawning([("coder", "impl", "M1")]),
            resume_done(lambda results: f"mgr<{results[0]}>"),
        ],
    )
    grandchild = ScriptedSession("coder", [terminal("leaf")])
    scheduler, events = build_scheduler(lead, [manager, grandchild])

    result = run(scheduler.run("deep delegate"))

    assert result == "lead<mgr<leaf>>"
    assert len(event_types(events, "agent_resumed")) == 2  # manager, then lead

def test_spawn_with_review_waits_for_nested_coder_terminal_result():
    lead = ScriptedSession("lead", [])
    coder = ScriptedSession(
        "coder",
        [
            suspend_spawning([("analyst", "inspect", "nested-1")]),
            resume_done(lambda results: f"final implementation from {results[0]}"),
        ],
    )
    analyst = ScriptedSession("analyst", [terminal("analysis")])
    reviewer = ScriptedSession("reviewer", [terminal("VERDICT: PASS")])
    scheduler, _ = build_scheduler(lead, [coder, analyst, reviewer])

    result = run(
        asyncio.wait_for(
            scheduler.spawn_with_review(0, "implement nested task", max_iterations=1),
            timeout=1,
        )
    )

    assert "PASSED after 1 iteration" in result
    assert "final implementation from analysis" in result
    assert "delegating" not in result

def test_wake_unknown_tool_call_id_raises():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )

    with pytest.raises(PendingRowError):
        run(scheduler._wake(0, "tc-unknown", "x", RowStatus.DONE))

def test_misrouted_completion_emits_failure_not_silent():
    lead = ScriptedSession("lead", [])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )
    # Register a bogus origin pointing at a tool_call_id that doesn't exist.
    scheduler._spawn_origin[99] = (0, "tc-bogus")

    run(scheduler._deliver_to_parent(99, "result", RowStatus.DONE))

    assert len(event_types(events, "agent_failed")) == 1

def test_send_message_queues_when_target_awaiting_events():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession("coder", [])
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    result = run(scheduler.send_message(0, 1, "question", "are you there?"))

    assert result == "Message queued to aid 1."
    assert target.added == []  # never delivered
    assert scheduler._message_inbox[1][0].content == "are you there?"

def test_queued_message_delivers_after_target_awaiting_events_resumes():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    target = ScriptedSession(
        "coder",
        [
            resume_done(lambda results: f"finished {results[0]}"),
            terminal("message handled"),
        ],
    )
    target.state.aid = 1
    target.scheduler = scheduler
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    target.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=7)
    )
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    async def scenario():
        await scheduler.send_message(0, 1, "follow up", "are you there?")
        await scheduler._wake(1, "tc-1", "child done", RowStatus.DONE)
        for _ in range(3):
            task = scheduler._tasks.get(1)
            if task is None:
                break
            await task
            if scheduler._tasks.get(1) is task:
                break

    run(scenario())

    assert target.added == [
        '<teammate-message teammate_id="A0" summary="follow up">\n'
        "are you there?\n"
        "</teammate-message>"
    ]
    assert scheduler.table.get(1).result == "message handled"

def test_child_message_to_suspended_parent_does_not_stall_turn():
    async def child_reports_progress(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(
            sess.state.aid,
            0,
            "progress",
            "analysis started",
        )
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "child final"})
        return "child final"

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("analyst", "investigate", "tc-1")]),
            resume_done(lambda results: f"lead saw {results[0]}"),
            terminal("progress message handled"),
        ],
    )
    child = ScriptedSession("analyst", [child_reports_progress])
    scheduler, events = build_scheduler(lead, [child])

    result = run(asyncio.wait_for(scheduler.run("go"), timeout=1))

    assert result == "progress message handled"
    assert scheduler._message_inbox.get(0) == []
    assert lead.added[-1] == (
        '<teammate-message teammate_id="A1" summary="progress">\n'
        "analysis started\n"
        "</teammate-message>"
    )
    assert len(event_types(events, "agent_message_sent")) == 1
    assert len(event_types(events, "agent_message_delivered")) == 1

def test_scheduler_run_retrieves_finished_task_exception(caplog):
    lead = ScriptedSession("lead", [terminal("done")])
    scheduler, _ = build_scheduler(lead, [])

    class FinishedTask:
        result_called = False

        def done(self):
            return True

        def result(self):
            self.result_called = True
            raise RuntimeError("background failure")

    finished = FinishedTask()
    scheduler._tasks[99] = finished

    assert run(scheduler.run("go")) == "done"
    assert finished.result_called is True
    assert "background task for aid 99 failed" in caplog.text

def test_scheduler_run_does_not_return_previous_turn_answer():
    async def precheck_stop(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.STOPPED)
        sess.state.append_message(
            {"role": "system", "content": "[Step limit reached. Session stopped.]"}
        )
        return ""

    lead = ScriptedSession("lead", [precheck_stop])
    lead.state.append_message({"role": "assistant", "content": "previous answer"})
    scheduler, _ = build_scheduler(lead, [])

    assert run(asyncio.wait_for(scheduler.run("new question"), timeout=0.5)) == ""

def test_concurrent_scheduler_runs_are_serialized_on_the_lead_session():
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def first_turn(sess: ScriptedSession) -> str:
        first_started.set()
        await first_release.wait()
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "first answer"})
        return "first answer"

    lead = ScriptedSession(
        "lead",
        [first_turn, terminal("second answer")],
    )
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        first_call = asyncio.create_task(scheduler.run("first question"))
        await first_started.wait()
        first_driver = scheduler._tasks[0]
        second_call = asyncio.create_task(scheduler.run("second question"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert lead.added == ["first question"]
        assert scheduler._tasks[0] is first_driver
        assert second_call.done() is False

        first_release.set()
        assert await asyncio.wait_for(first_call, timeout=0.5) == "first answer"
        assert await asyncio.wait_for(second_call, timeout=0.5) == "second answer"
        assert lead.added == ["first question", "second question"]

    run(scenario())
