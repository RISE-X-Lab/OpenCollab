"""Scheduler cleanup race and resource release tests."""

from __future__ import annotations

import asyncio

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    run,
    terminal,
)

from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState


def test_cleanup_wins_race_after_delivery_starts_before_parent_row_fill():
    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [terminal("late success")])
    scheduler, _ = build_scheduler(lead, [child])
    real_wake = scheduler._wake
    wake_entered = asyncio.Event()
    release_wake = asyncio.Event()
    cancellations = 0

    async def gated_wake(*args, **kwargs):
        nonlocal cancellations
        wake_entered.set()
        while not release_wake.is_set():
            try:
                await release_wake.wait()
            except asyncio.CancelledError:
                cancellations += 1
        return await real_wake(*args, **kwargs)

    scheduler._wake = gated_wake

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        aid = await scheduler.spawn(
            0,
            "coder",
            "finish during cleanup",
            tool_call_id="delivery-race",
        )
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="delivery-race",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
            )
        )
        driver = scheduler._tasks[aid]
        await wake_entered.wait()
        assert aid in scheduler._spawn_origin

        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )
        row = lead.state.pending_events.rows["delivery-race"]
        assert cancellations >= 1
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
        assert scheduler.table.get(aid).result.startswith("Error: scheduler cleanup")
        assert aid not in scheduler._spawn_origin

        release_wake.set()
        await asyncio.wait_for(driver, timeout=0.5)
        row = lead.state.pending_events.rows["delivery-race"]
        assert row.status is RowStatus.FAILED
        assert row.result and "scheduler cleanup" in row.result
        assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED

    run(scenario())

def test_cleanup_is_bounded_when_worktree_release_ignores_cancellation():
    class StubbornReleasePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.release_gate = asyncio.Event()
            self.finished = asyncio.Event()
            self.cancellations = 0
            self.release_calls = 0

        async def acquire(self, role):
            return None

        async def release(self):
            self.release_calls += 1
            self.started.set()
            while not self.release_gate.is_set():
                try:
                    await self.release_gate.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.finished.set()

    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    pool = StubbornReleasePool()
    scheduler._worktree_pool = pool

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: worktree pool release failed or timed out",
        ):
            await asyncio.wait_for(
                scheduler.cleanup(cleanup_timeout=0.01),
                timeout=0.5,
            )
        assert pool.started.is_set()
        assert pool.cancellations >= 1
        assert pool.release_calls == 1
        pool.release_gate.set()
        await asyncio.wait_for(pool.finished.wait(), timeout=0.5)

    run(scenario())

def test_cleanup_surfaces_synchronous_worktree_release_failure():
    error = OSError("pool release failed")

    class FailingReleasePool:
        async def acquire(self, role):
            return None

        def release(self):
            raise error

    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    scheduler._worktree_pool = FailingReleasePool()

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: worktree pool release failed or timed out",
        ) as caught:
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert "worktree pool" in str(caught.value)

    run(scenario())

def test_cleanup_surfaces_environment_abort_failure():
    class FailingAbortEnv:
        def __init__(self):
            self.revoked = False

        def revoke(self):
            self.revoked = True

        async def abort(self):
            raise OSError("abort failed")

    class ResistantSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_loop(self):
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            return "late"

    env = FailingAbortEnv()
    child = ResistantSession(env)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "stay active")
        driver = scheduler._tasks[aid]
        await child.started.wait()
        with pytest.raises(RuntimeError) as caught:
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert "session environment abort failed or timed out" in str(caught.value)
        assert env.revoked is True
        assert scheduler.table.get(aid).state.phase is SessionPhase.STOPPED
        child.release.set()
        await asyncio.wait_for(driver, timeout=0.5)

    run(scenario())

def test_cleanup_uses_public_revoke_on_slotted_environment():
    class SlottedEnvironment:
        __slots__ = ("abort_calls", "revoked")

        def __init__(self):
            self.revoked = False
            self.abort_calls = 0

        def revoke(self):
            self.revoked = True

        async def abort(self):
            self.abort_calls += 1

    environment = SlottedEnvironment()
    lead = ScriptedSession("lead", [])
    lead.env = environment
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        assert await scheduler._abort_session_environments({0}, timeout=0.1)

    run(scenario())
    assert environment.revoked is True
    assert environment.abort_calls == 1

def test_spawn_blocked_in_acquire_cannot_resurrect_after_cleanup():
    class BlockingAcquirePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.gate = asyncio.Event()
            self.release_calls = 0
            self.release_env_calls = 0
            self.acquire_calls = 0
            self.cancellations = 0
            self.env = object()

        async def acquire(self, role):
            self.acquire_calls += 1
            self.started.set()
            while not self.gate.is_set():
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            return self.env

        async def release(self):
            self.release_calls += 1

        async def release_env(self, env):
            assert env is self.env
            self.release_env_calls += 1

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [terminal("must never start")])
    scheduler, _ = build_scheduler(lead, [child])
    pool = BlockingAcquirePool()
    scheduler._worktree_pool = pool

    async def scenario():
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="startup-race",
                kind=RowKind.CHILD_AGENT,
                order=0,
            )
        )
        spawn_task = asyncio.create_task(
            scheduler.spawn(
                0,
                "coder",
                "blocked startup",
                tool_call_id="startup-race",
            )
        )
        await pool.started.wait()
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: execution tasks did not quiesce",
        ):
            await scheduler.cleanup(cleanup_timeout=0.01)
        assert pool.release_calls == 0
        assert pool.cancellations >= 1
        assert scheduler._startup_tasks == {}
        assert scheduler._startup_origin == {}
        assert scheduler._startup_envs == {}
        assert scheduler._child_lease == {}
        assert scheduler.inflight_spawn("coder", "blocked startup") is None
        startup_row = lead.state.pending_events.rows["startup-race"]
        assert startup_row.status is RowStatus.FAILED
        assert startup_row.error and "scheduler cleanup" in startup_row.error

        pool.gate.set()
        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await spawn_task

        assert set(scheduler.table.entries) == {0}
        assert set(scheduler._sessions) == {0}
        assert scheduler._tasks == {}
        assert scheduler._child_lease == {}
        assert scheduler.inflight_spawn("coder", "blocked startup") is None
        assert pool.release_env_calls == 1

        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await scheduler.spawn(0, "coder", "after cleanup")
        assert pool.acquire_calls == 1
        with pytest.raises(RuntimeError, match="scheduler is shutting down"):
            await scheduler.run("after cleanup")

    run(scenario())

def test_message_add_blocked_during_cleanup_cannot_create_late_driver():
    class BlockingAddSession(ScriptedSession):
        def __init__(self):
            super().__init__("coder", [terminal("first turn")])
            self.add_started = asyncio.Event()
            self.add_release = asyncio.Event()

        async def add_user_message(self, content):
            self.added.append(content)
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            self.add_started.set()
            await self.add_release.wait()

    lead = ScriptedSession("lead", [])
    child = BlockingAddSession()
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "first")
        await scheduler._tasks[aid]
        assert child.state.phase is SessionPhase.DONE
        child.state.turn.recent_call_hashes = ["prior-call"]
        child.state.turn.reads_since_last_edit = 4
        child.state.turn.low_yield_since_progress = 3
        child.state.turn.distinct_evidence_count = 2
        child.state.turn.steps_since_progress = 5
        child.state.turn.loop_blocked_since_progress = 1
        child.state.turn.seen_result_hashes = {"prior-result"}
        child.state.turn.scout_ledger = [{"tool": "grep", "outcome": "hit"}]
        messages_before = list(child.state.messages)

        send_task = asyncio.create_task(
            scheduler.send_message(0, aid, "late", "must stay queued")
        )
        await child.add_started.wait()
        assert len(scheduler._message_inbox[aid]) == 1
        assert len(child.state.pending_user_messages) == 1
        with pytest.raises(
            RuntimeError,
            match="technical scheduler cleanup failed: message delivery was interrupted",
        ):
            await scheduler.cleanup(cleanup_timeout=0.01)
        with pytest.raises(asyncio.CancelledError):
            await send_task

        assert scheduler._message_delivery_tasks == {}
        assert scheduler._tasks == {}
        assert child.state.phase is SessionPhase.DONE
        assert child.state.messages == messages_before
        assert child.state.turn.recent_call_hashes == ["prior-call"]
        assert child.state.turn.reads_since_last_edit == 4
        assert child.state.turn.low_yield_since_progress == 3
        assert child.state.turn.distinct_evidence_count == 2
        assert child.state.turn.steps_since_progress == 5
        assert child.state.turn.loop_blocked_since_progress == 1
        assert child.state.turn.seen_result_hashes == {"prior-result"}
        assert child.state.turn.scout_ledger == [{"tool": "grep", "outcome": "hit"}]
        assert len(scheduler._message_inbox[aid]) == 1
        assert len(child.state.pending_user_messages) == 1
        assert aid not in scheduler._child_lease

    run(scenario())
