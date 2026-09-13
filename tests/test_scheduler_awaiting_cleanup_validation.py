"""Scheduler cleanup validation and terminal wait tests."""

from __future__ import annotations

import asyncio

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    run,
    terminal,
)

from opencollab.application.scheduler import SchedulerStalledError
from opencollab.domain.session import SessionPhase, SessionState


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -0.1, float("inf"), float("-inf"), float("nan"), True, "invalid", None],
)
def test_cleanup_rejects_invalid_timeout_before_any_side_effect(invalid_timeout):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_child(sess: ScriptedSession) -> str:
        started.set()
        await release.wait()
        sess.state.set_phase(SessionPhase.DONE)
        return "done"

    class RecordingPool:
        def __init__(self):
            self.release_calls = 0

        async def acquire(self, role):
            return None

        async def release(self):
            self.release_calls += 1

    lead = ScriptedSession("lead", [])
    child = ScriptedSession("coder", [blocking_child])
    scheduler, _ = build_scheduler(lead, [child])
    pool = RecordingPool()
    scheduler._worktree_pool = pool

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "keep running")
        await started.wait()
        driver = scheduler._tasks[aid]
        with pytest.raises(
            ValueError,
            match="cleanup_timeout must be a finite number greater than zero",
        ):
            await scheduler.cleanup(cleanup_timeout=invalid_timeout)

        assert scheduler._shutting_down is False
        assert scheduler._tasks[aid] is driver
        assert driver.done() is False
        assert aid in scheduler._turn_lease
        assert pool.release_calls == 0

        release.set()
        await asyncio.wait_for(driver, timeout=0.5)

    run(scenario())


def test_wait_until_terminal_fails_without_active_producer():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    with pytest.raises(SchedulerStalledError, match="aid 0"):
        run(scheduler.wait_until_terminal(0))


def test_active_startup_prevents_quiescence_until_child_finishes():
    class BlockingAcquirePool:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def acquire(self, role):
            self.started.set()
            await self.release.wait()
            return None

        async def release(self):
            return None

    lead = ScriptedSession("lead", [terminal("lead done")])
    child = ScriptedSession("coder", [terminal("child done")])
    scheduler, _ = build_scheduler(lead, [child])
    pool = BlockingAcquirePool()
    scheduler._worktree_pool = pool

    async def scenario():
        startup = asyncio.create_task(scheduler.spawn(0, "coder", "blocked startup"))
        await pool.started.wait()
        assert scheduler._quiescent() is False

        turn = asyncio.create_task(scheduler.run("finish the lead turn"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert turn.done() is False

        pool.release.set()
        aid = await startup
        assert await asyncio.wait_for(turn, timeout=0.5) == "lead done"
        assert scheduler.table.get(aid).state.phase is SessionPhase.DONE

    run(scenario())

def test_wait_until_terminal_follows_message_replacement_created_by_finishing_task():
    class BlockingDiff:
        def __init__(self):
            self.release = asyncio.Event()

        async def get_diff(self):
            await self.release.wait()
            return ""

    class TwoTurnSession:
        def __init__(self, env):
            self.agent = type("_Agent", (), {"name": "coder"})()
            self.state = SessionState(messages=[])
            self.used_tokens = 0
            self.env = env
            self.calls = 0
            self.first_terminal = asyncio.Event()
            self.second_started = asyncio.Event()
            self.second_release = asyncio.Event()

        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()

        async def run_loop(self):
            self.calls += 1
            if self.calls == 1:
                self.state.set_phase(SessionPhase.DONE)
                self.first_terminal.set()
                return "first"
            self.second_started.set()
            await self.second_release.wait()
            self.state.set_phase(SessionPhase.DONE)
            return "second"

    diff = BlockingDiff()
    child = TwoTurnSession(diff)
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await child.first_terminal.wait()
        await scheduler.send_message(0, aid, "followup", "continue")
        waiter = asyncio.create_task(scheduler.wait_until_terminal(aid))
        await asyncio.sleep(0)
        diff.release.set()
        await child.second_started.wait()
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not scheduler._tasks[aid].done()
        child.second_release.set()
        await asyncio.wait_for(waiter, timeout=0.5)
        return aid

    aid = run(scenario())

    assert child.calls == 2
    assert scheduler.table.get(aid).result == "second"
