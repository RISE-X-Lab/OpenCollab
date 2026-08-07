"""Scheduler cleanup lifecycle tests."""

from __future__ import annotations

import asyncio

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    resume_done,
    run,
    terminal,
)

from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState


def test_scheduler_run_cancellation_tears_down_owned_team_before_propagating():
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_turn(sess: ScriptedSession) -> str:
        started.set()
        await never_release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [blocking_turn])
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        run_task = asyncio.create_task(scheduler.run("cancel this turn"))
        await started.wait()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=0.5)

        assert scheduler._shutting_down is True
        assert scheduler._tasks == {}
        assert scheduler.table.get(0).state.phase is SessionPhase.STOPPED
        assert scheduler._lead_lease is None

    run(scenario())

def test_cleanup_marks_interrupted_lead_message_as_technical_failure():
    class BlockingLead(ScriptedSession):
        def __init__(self):
            super().__init__("lead", [terminal("must never run")])
            self.add_started = asyncio.Event()
            self.add_release = asyncio.Event()

        async def add_user_message(self, content):
            self.added.append(content)
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            self.add_started.set()
            await self.add_release.wait()

    lead = BlockingLead()
    lead.state.turn.recent_call_hashes = ["lead-call"]
    lead.state.turn.reads_since_last_edit = 3
    lead.state.turn.low_yield_since_progress = 2
    lead.state.turn.distinct_evidence_count = 1
    lead.state.turn.steps_since_progress = 4
    lead.state.turn.loop_blocked_since_progress = 1
    lead.state.turn.seen_result_hashes = {"lead-result"}
    lead.state.turn.scout_ledger = [{"tool": "file_read", "outcome": "hit"}]
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        run_task = asyncio.create_task(scheduler.run("blocked lead add"))
        await lead.add_started.wait()
        with pytest.raises(RuntimeError, match="message delivery was interrupted"):
            await scheduler.cleanup(cleanup_timeout=0.01)
        with pytest.raises(asyncio.CancelledError):
            await run_task

    run(scenario())
    assert scheduler._tasks == {}
    assert scheduler._lead_lease is None
    assert scheduler.table.get(0).state.phase is SessionPhase.STOPPED
    assert lead.state.messages == []
    assert lead.state.message_timestamps == []
    assert lead.state.turn.recent_call_hashes == ["lead-call"]
    assert lead.state.turn.reads_since_last_edit == 3
    assert lead.state.turn.low_yield_since_progress == 2
    assert lead.state.turn.distinct_evidence_count == 1
    assert lead.state.turn.steps_since_progress == 4
    assert lead.state.turn.loop_blocked_since_progress == 1
    assert lead.state.turn.seen_result_hashes == {"lead-result"}
    assert lead.state.turn.scout_ledger == [{"tool": "file_read", "outcome": "hit"}]

def test_running_cancelled_child_fails_parent_row_and_resumes_parent():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        return "unreachable"

    lead = ScriptedSession(
        "lead", [resume_done(lambda results: f"resumed with {results[0]}")]
    )
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(0, "coder", "cancel me", tool_call_id="cancel-me")
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="cancel-me",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        await started.wait()
        scheduler._tasks[aid].cancel()
        with pytest.raises(asyncio.CancelledError):
            await scheduler._tasks[aid]
        await scheduler.wait_until_terminal(0)
        return aid

    aid = run(scenario())

    assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
    assert scheduler.table.get(aid).result.startswith("Error: agent cancelled")
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    assert "Error: agent cancelled" in scheduler.table.get(0).result
    assert aid not in scheduler._spawn_origin

def test_cancelled_spawn_before_driver_fails_parent_row_without_ghost_child():
    class BlockingPool:
        def __init__(self):
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def acquire(self, role):
            self.started.set()
            await self.gate.wait()

        async def release(self):
            return None

    lead = ScriptedSession(
        "lead", [resume_done(lambda results: f"startup failed: {results[0]}")]
    )
    scheduler, _ = build_scheduler(lead, [])
    pool = BlockingPool()
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="startup-cancel",
                kind=RowKind.CHILD_AGENT,
                order=0,
            )
        )
        task = asyncio.create_task(
            scheduler.spawn(
                0,
                "coder",
                "cancel during acquire",
                tool_call_id="startup-cancel",
            )
        )
        await pool.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await scheduler.wait_until_terminal(0)

    run(scenario())

    assert set(scheduler.table.entries) == {0}
    assert set(scheduler._sessions) == {0}
    assert scheduler._spawn_origin == {}
    assert lead.state.pending_events.is_empty()
    assert "spawn cancelled before startup" in scheduler.table.get(0).result

def test_cleanup_cancellation_does_not_start_message_replacement_task():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child, terminal("message handled")])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "long task")
        await started.wait()
        await scheduler.send_message(0, aid, "queued", "do this next")
        await scheduler.cleanup()
        return aid

    aid = run(scenario())

    assert scheduler._shutting_down is True
    assert scheduler._tasks == {}
    assert child.state.phase is SessionPhase.STOPPED
    assert scheduler._message_inbox[aid]

def test_cleanup_finalizes_driver_cancelled_before_first_timeslice():
    never_release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        await never_release.wait()
        return "unreachable"

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "cancel immediately")
        assert aid in scheduler._child_lease
        await scheduler.cleanup()
        return aid

    aid = run(scenario())

    assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
    assert aid not in scheduler._child_lease
    assert scheduler.inflight_spawn("coder", "cancel immediately") is None
    assert aid not in scheduler._spawn_origin

def test_cleanup_releases_seeded_lead_lease_without_an_active_turn():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    assert scheduler._lead_lease is not None
    assert scheduler.allocated_tokens > 0

    run(scheduler.cleanup(cleanup_timeout=0.01))

    assert scheduler._lead_lease is None
    assert scheduler.allocated_tokens == 0

def test_cleanup_is_bounded_when_session_ignores_both_cancellations():
    class AbortTrackingEnv:
        def __init__(self):
            self.aborted = asyncio.Event()

        async def abort(self):
            self.aborted.set()

    class RecordingPool:
        def __init__(self, env):
            self.env = env
            self.released = asyncio.Event()

        async def acquire(self, role):
            return self.env

        async def release(self):
            self.released.set()

    class StubbornSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()
            self.cancellations = 0

        async def run_loop(self):
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.finished.set()
            return "late success must be discarded"

    env = AbortTrackingEnv()
    pool = RecordingPool(env)
    child = StubbornSession(env)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "ignore cancellation",
            tool_call_id="stubborn-child",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="stubborn-child",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        await child.started.wait()
        driver = scheduler._tasks[aid]
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )

        row = lead.state.pending_events.rows["stubborn-child"]
        assert env.aborted.is_set()
        assert pool.released.is_set() is False
        assert child.cancellations >= 1
        assert row.status is RowStatus.FAILED
        assert row.error and "scheduler cleanup" in row.error
        assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")
        assert aid not in scheduler._child_lease
        assert scheduler.inflight_spawn("coder", "ignore cancellation") is None
        assert scheduler._tasks == {}

        child.release.set()
        await asyncio.wait_for(child.finished.wait(), timeout=0.5)
        await asyncio.wait_for(driver, timeout=0.5)
        assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")

    run(scenario())

def test_cleanup_caller_cancellation_waits_for_owned_teardown_then_propagates():
    class BlockingReleasePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.release_gate = asyncio.Event()
            self.finished = False

        async def acquire(self, role):
            return None

        async def release(self):
            self.started.set()
            await self.release_gate.wait()
            self.finished = True

    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    pool = BlockingReleasePool()
    scheduler._worktree_pool = pool

    async def scenario():
        cleanup_task = asyncio.create_task(scheduler.cleanup(cleanup_timeout=0.2))
        await pool.started.wait()
        cleanup_task.cancel()
        pool.release_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup_task, timeout=0.5)
        assert pool.finished is True

    run(scenario())


def test_completed_direct_spawns_release_driver_task_references():
    lead = ScriptedSession("lead", [])
    children = [
        ScriptedSession("coder", [terminal(f"result-{index}")])
        for index in range(100)
    ]
    scheduler, _ = build_scheduler(lead, children)

    async def scenario():
        for index in range(100):
            aid = await scheduler.spawn(0, "coder", f"task-{index}")
            await scheduler.wait_until_terminal(aid)

        await asyncio.sleep(0)
        assert scheduler._tasks == {}

    run(scenario())


def test_completed_deferred_delivery_releases_commit_marker():
    lead = ScriptedSession(
        "lead",
        [resume_done(lambda results: f"received: {results[0]}")],
    )
    child = ScriptedSession("coder", [terminal("child result")])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "deliver once",
            tool_call_id="child-call",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="child-call",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )

        await scheduler.wait_until_terminal(aid)
        await scheduler.wait_until_terminal(0)
        await asyncio.sleep(0)

        assert scheduler._tasks == {}
        assert scheduler._delivery_committed == set()
        row = lead.state.pending_events.rows.get("child-call")
        assert row is None
        assert scheduler.table.get(0).result == "received: child result"

    run(scenario())
