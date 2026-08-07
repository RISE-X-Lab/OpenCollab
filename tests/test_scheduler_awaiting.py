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

from opencollab.application.scheduler import SchedulerTurnError
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


def test_deferred_parent_completion_latency_includes_child_wait():
    async def delayed_child(sess: ScriptedSession) -> str:
        await asyncio.sleep(0.03)
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "child output"})
        return "child output"

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [delayed_child])
    scheduler, events = build_scheduler(lead, [child])

    assert run(scheduler.run("please delegate")) == "final: child output"

    lead_completions = [
        event
        for event in event_types(events, "agent_completed")
        if event.data["aid"] == 0
    ]
    assert len(lead_completions) == 1
    assert lead_completions[0].data["latency"] >= 0.02


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


def test_targeted_cancel_closes_nested_wake_gates_before_leaf_delivery():
    grandchild_started = asyncio.Event()
    release_grandchild = asyncio.Event()
    intermediate_resumed = asyncio.Event()
    release_intermediate_resume = asyncio.Event()

    class CancelAwareSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            step = self._steps.pop(0)
            return await step(self, cancel_event)

    def suspend_on(role: str, task: str, tool_call_id: str):
        async def step(sess, _cancel_event):
            child_aid = await sess.scheduler.spawn(
                sess.state.aid,
                role,
                task,
                tool_call_id=tool_call_id,
            )
            sess.state.pending_events.add(
                PendingRow(
                    tool_call_id=tool_call_id,
                    kind=RowKind.CHILD_AGENT,
                    order=0,
                    ref=child_aid,
                )
            )
            sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
            return ""

        return step

    async def unexpected_intermediate_resume(sess, _cancel_event):
        intermediate_resumed.set()
        await release_intermediate_resume.wait()
        sess.state.cancel("parent turn interrupted by user")
        return ""

    async def blocked_grandchild(sess, _cancel_event):
        grandchild_started.set()
        await release_grandchild.wait()
        sess.state.mark_done()
        return "late grandchild result"

    lead = CancelAwareSession(
        "lead",
        [suspend_on("manager", "nested task", "lead-child")],
    )
    child = CancelAwareSession(
        "manager",
        [
            suspend_on("coder", "blocked leaf", "child-grandchild"),
            unexpected_intermediate_resume,
        ],
    )
    grandchild = CancelAwareSession("coder", [blocked_grandchild])
    scheduler, _ = build_scheduler(lead, [child, grandchild])

    async def scenario():
        cancel_event = asyncio.Event()
        call = asyncio.create_task(
            scheduler.run("delegate deeply", cancel_event=cancel_event)
        )
        await asyncio.wait_for(grandchild_started.wait(), timeout=0.5)

        cancel_event.set()
        done, _pending = await asyncio.wait({call}, timeout=0.15)
        finished_before_release = call in done
        resumed_before_release = intermediate_resumed.is_set()
        live_before_release = [
            task
            for task in (
                *scheduler._tasks.values(),
                *scheduler._startup_tasks.values(),
                *scheduler._message_delivery_tasks.values(),
            )
            if not task.done()
        ]

        release_grandchild.set()
        release_intermediate_resume.set()
        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await asyncio.wait_for(call, timeout=0.5)

        assert finished_before_release is True
        assert resumed_before_release is False
        assert live_before_release == []
        assert all(
            scheduler.table.get(descendant).state.phase is SessionPhase.STOPPED
            for descendant in (0, 1, 2)
        )
        assert scheduler._shutting_down is False

    run(scenario())


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


@pytest.mark.parametrize("phase", (SessionPhase.ERROR, SessionPhase.STOPPED))
def test_spawn_with_review_rejects_noncompleted_coder_despite_reviewer_pass(phase):
    async def incomplete_coder(session: ScriptedSession) -> str:
        session.state.set_phase(phase)
        return f"coder ended in {phase.value}"

    lead = ScriptedSession("lead", [])
    coder = ScriptedSession("coder", [incomplete_coder])
    reviewer = ScriptedSession("reviewer", [terminal("VERDICT: PASS")])
    scheduler, _ = build_scheduler(lead, [coder, reviewer])

    result = run(scheduler.spawn_with_review(0, "implement safely", max_iterations=1))

    assert result.startswith("[Self-Collaboration: FAILED")
    assert f"Coder terminal phase: {phase.value}" in result
    assert "PASSED" not in result
    assert len(scheduler.table.entries) == 2

def test_wake_unknown_tool_call_id_raises():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=5)
    )

    with pytest.raises(PendingRowError):
        run(scheduler._wake(0, "tc-unknown", "x", RowStatus.DONE))

def test_unknown_completion_route_fails_open_parent_batch_in_one_delivery():
    async def resume_after_routing_failure(sess: ScriptedSession) -> str:
        row = sess.state.pending_events.rows["unrelated"]
        assert row.status is RowStatus.FAILED
        assert row.result is not None
        assert "routing failed" in row.result
        assert row.result != "child result"
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        return "parent recovered"

    lead = ScriptedSession("lead", [resume_after_routing_failure])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="unrelated", kind=RowKind.CHILD_AGENT, order=0, ref=42)
    )
    scheduler._spawn_origin[99] = (0, "tc-bogus")

    async def scenario():
        await scheduler._deliver_to_parent(99, "child result", RowStatus.DONE)
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert len(event_types(events, "agent_failed")) == 1
    assert scheduler._spawn_origin == {}
    assert scheduler.table.get(0).result == "parent recovered"


def test_misrouted_completion_retargets_the_unique_child_row():
    lead = ScriptedSession("lead", [resume_done(lambda results: f"recovered: {results[0]}")])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="actual", kind=RowKind.CHILD_AGENT, order=0, ref=99)
    )
    scheduler._spawn_origin[99] = (0, "stale")

    async def scenario():
        await scheduler._deliver_to_parent(99, "result", RowStatus.DONE)
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert event_types(events, "agent_failed") == []
    assert scheduler._spawn_origin == {}
    assert scheduler.table.get(0).result == "recovered: result"


def test_already_filled_claimed_row_retargets_unique_pending_child_row():
    async def resume_after_retarget(sess: ScriptedSession) -> str:
        already = sess.state.pending_events.rows["already-filled"]
        assert already.status is RowStatus.DONE
        assert already.result == "old result"
        actual = sess.state.pending_events.rows["actual"]
        assert actual.status is RowStatus.DONE
        assert actual.result == "child result"
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        return "parent recovered"

    lead = ScriptedSession("lead", [resume_after_retarget])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="already-filled", kind=RowKind.CHILD_AGENT, order=0, ref=7)
    )
    lead.state.pending_events.fill(
        "already-filled",
        result="old result",
        status=RowStatus.DONE,
    )
    lead.state.pending_events.add(
        PendingRow(tool_call_id="actual", kind=RowKind.CHILD_AGENT, order=1, ref=99)
    )
    scheduler._spawn_origin[99] = (0, "already-filled")

    async def scenario():
        await scheduler._deliver_to_parent(99, "child result", RowStatus.DONE)
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert event_types(events, "agent_failed") == []
    assert scheduler._spawn_origin == {}
    assert scheduler.table.get(0).result == "parent recovered"


def test_ambiguous_completion_route_fails_open_parent_batch_in_one_delivery():
    async def resume_after_routing_failure(sess: ScriptedSession) -> str:
        for tool_call_id in ("duplicate-a", "duplicate-b", "unrelated"):
            row = sess.state.pending_events.rows[tool_call_id]
            assert row.status is RowStatus.FAILED
            assert row.result is not None
            assert "routing failed" in row.result
            assert row.result != "child result"
        sess.state.pending_events.clear()
        sess.state.set_phase(SessionPhase.DONE)
        return "parent recovered"

    lead = ScriptedSession("lead", [resume_after_routing_failure])
    scheduler, events = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
    lead.state.pending_events.add(
        PendingRow(tool_call_id="duplicate-a", kind=RowKind.CHILD_AGENT, order=0, ref=99)
    )
    lead.state.pending_events.add(
        PendingRow(tool_call_id="duplicate-b", kind=RowKind.CHILD_AGENT, order=1, ref=99)
    )
    lead.state.pending_events.add(
        PendingRow(tool_call_id="unrelated", kind=RowKind.CHILD_AGENT, order=2, ref=42)
    )
    scheduler._spawn_origin[99] = (0, "tc-bogus")

    async def scenario():
        await scheduler._deliver_to_parent(99, "child result", RowStatus.DONE)
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert len(event_types(events, "agent_failed")) == 1
    assert scheduler._spawn_origin == {}
    assert scheduler.table.get(0).result == "parent recovered"

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

    assert len(target.added) == 1
    assert target.added[0].startswith(
        '<teammate-message teammate_id="A0" summary="follow up" message_id="'
    )
    assert "are you there?\n</teammate-message>" in target.added[0]
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
    assert lead.added[-1].startswith(
        '<teammate-message teammate_id="A1" summary="progress" message_id="'
    )
    assert "analysis started\n</teammate-message>" in lead.added[-1]
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


@pytest.mark.parametrize(
    ("existing_reason", "expected_reason"),
    [
        ("ProviderFailure: retry budget exhausted", "ProviderFailure: retry budget exhausted"),
        (None, "ProviderFailure: upstream 429"),
    ],
)
def test_scheduler_run_preserves_session_failure_reason_after_exception(
    existing_reason, expected_reason
):
    class ProviderFailure(RuntimeError):
        pass

    async def fail_with_provider_reason(sess: ScriptedSession) -> str:
        if existing_reason is not None:
            sess.state.fail(existing_reason)
        raise ProviderFailure("upstream 429")

    lead = ScriptedSession("lead", [fail_with_provider_reason])
    scheduler, _ = build_scheduler(lead, [])

    with pytest.raises(SchedulerTurnError, match=expected_reason) as caught:
        run(asyncio.wait_for(scheduler.run("new question"), timeout=0.5))
    assert caught.value.phase is SessionPhase.ERROR
    assert caught.value.terminal_reason == expected_reason


@pytest.mark.parametrize(
    ("phase", "reason"),
    [
        (SessionPhase.STOPPED, "step limit reached"),
        (SessionPhase.ERROR, "provider failed"),
    ],
)
def test_scheduler_run_does_not_return_partial_answer_on_terminal_failure(phase, reason):
    async def precheck_stop(sess: ScriptedSession) -> str:
        sess.state.set_phase(phase)
        sess.state.terminal_reason = reason
        sess.state.append_message(
            {"role": "system", "content": "[Step limit reached. Session stopped.]"}
        )
        sess.state.append_message({"role": "assistant", "content": "partial answer"})
        return "partial answer"

    lead = ScriptedSession("lead", [precheck_stop])
    lead.state.append_message({"role": "assistant", "content": "previous answer"})
    scheduler, _ = build_scheduler(lead, [])

    with pytest.raises(SchedulerTurnError, match=f"{phase.value}: {reason}") as caught:
        run(asyncio.wait_for(scheduler.run("new question"), timeout=0.5))
    assert caught.value.aid == 0
    assert caught.value.phase is phase
    assert caught.value.partial_answer == "partial answer"

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


def test_external_turns_for_different_agents_start_concurrently():
    release = asyncio.Event()
    lead_started = asyncio.Event()
    child_started = asyncio.Event()

    def gated_turn(started, answer):
        async def turn(sess: ScriptedSession) -> str:
            started.set()
            await release.wait()
            sess.state.set_phase(SessionPhase.DONE)
            sess.state.append_message(
                {"role": "assistant", "content": answer}
            )
            return answer

        return turn

    lead = ScriptedSession("lead", [gated_turn(lead_started, "lead answer")])
    child = ScriptedSession(
        "coder", [gated_turn(child_started, "child answer")]
    )
    child.state.aid = 1
    scheduler, _ = build_scheduler(lead, [])
    scheduler.table.add(
        SessionControlBlock(
            aid=1,
            parent_aid=0,
            agent=child.agent,
            state=child.state,
        )
    )
    scheduler._sessions[1] = child
    scheduler._reserve_child_budget(1)

    async def scenario():
        lead_call = asyncio.create_task(scheduler.run_turn(0, "lead task"))
        await lead_started.wait()
        child_call = asyncio.create_task(
            scheduler.run_turn(1, "child task")
        )
        try:
            await asyncio.wait_for(child_started.wait(), timeout=0.1)
        finally:
            release.set()
            results = await asyncio.gather(
                lead_call,
                child_call,
                return_exceptions=True,
            )
        assert results == ["lead answer", "child answer"]

    run(scenario())


def test_targeted_cancel_event_stops_only_addressed_agent_and_scheduler_reuses_it():
    lead_started = asyncio.Event()
    lead_release = asyncio.Event()
    child_started = asyncio.Event()
    child_stopped = asyncio.Event()

    class CancelAwareSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            step = self._steps.pop(0)
            return await step(self, cancel_event)

    async def lead_first(sess, _cancel_event):
        lead_started.set()
        await lead_release.wait()
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "lead answer"})
        return "lead answer"

    async def child_cancelled(sess, cancel_event):
        child_started.set()
        assert cancel_event is not None
        await cancel_event.wait()
        sess.state.cancel("interrupted by user")
        child_stopped.set()
        return ""

    async def child_retry(sess, cancel_event):
        assert cancel_event is None
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "retry answer"})
        return "retry answer"

    lead = CancelAwareSession("lead", [lead_first])
    child = CancelAwareSession("coder", [child_cancelled, child_retry])
    child.state.aid = 1
    scheduler, _ = build_scheduler(lead, [])
    scheduler.table.add(
        SessionControlBlock(
            aid=1,
            parent_aid=0,
            agent=child.agent,
            state=child.state,
        )
    )
    scheduler._sessions[1] = child
    scheduler._reserve_child_budget(1)

    async def scenario():
        lead_call = asyncio.create_task(scheduler.run_turn(0, "lead task"))
        await asyncio.wait_for(lead_started.wait(), timeout=0.5)
        cancel_event = asyncio.Event()
        child_call = asyncio.create_task(
            scheduler.run_turn(1, "child task", cancel_event=cancel_event)
        )
        await asyncio.wait_for(child_started.wait(), timeout=0.5)

        cancel_event.set()
        await asyncio.wait_for(child_stopped.wait(), timeout=0.5)
        assert lead.state.phase is not SessionPhase.STOPPED
        assert scheduler._shutting_down is False

        lead_release.set()
        assert await lead_call == "lead answer"
        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await child_call

        assert await scheduler.run_turn(1, "retry task") == "retry answer"
        assert scheduler._shutting_down is False

    run(scenario())


def test_targeted_cancel_event_settles_suspended_descendants_before_they_finish():
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    class CancelAwareSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            step = self._steps.pop(0)
            return await step(self, cancel_event)

    async def suspend_on_child(sess, _cancel_event):
        child_aid = await sess.scheduler.spawn(
            sess.state.aid,
            "coder",
            "blocked child",
            tool_call_id="blocked-child",
        )
        sess.state.pending_events.add(
            PendingRow(
                tool_call_id="blocked-child",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=child_aid,
            )
        )
        sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
        return ""

    async def resume_or_retry(sess, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            sess.state.pending_events.clear()
            sess.state.cancel("interrupted by user")
            return ""
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "retry answer"})
        return "retry answer"

    async def retry_done(sess, cancel_event):
        assert cancel_event is None
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "retry answer"})
        return "retry answer"

    async def blocked_child(sess):
        child_started.set()
        await release_child.wait()
        sess.state.mark_done()
        return "late child result"

    lead = CancelAwareSession(
        "lead",
        [suspend_on_child, resume_or_retry, retry_done],
    )
    child = ScriptedSession("coder", [blocked_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        cancel_event = asyncio.Event()
        call = asyncio.create_task(
            scheduler.run("delegate then cancel", cancel_event=cancel_event)
        )
        await asyncio.wait_for(child_started.wait(), timeout=0.5)
        assert lead.state.phase is SessionPhase.AWAITING_EVENTS

        cancel_event.set()
        for _ in range(30):
            await asyncio.sleep(0)
            if (
                lead.state.phase is SessionPhase.STOPPED
                and child.state.phase is SessionPhase.STOPPED
                and lead.state.pending_events.is_empty()
            ):
                break
        settled_before_release = (
            lead.state.phase is SessionPhase.STOPPED
            and child.state.phase is SessionPhase.STOPPED
            and lead.state.pending_events.is_empty()
        )

        release_child.set()
        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await asyncio.wait_for(call, timeout=0.5)

        assert settled_before_release is True
        assert scheduler._shutting_down is False
        assert await scheduler.run("retry") == "retry answer"

    run(scenario())
