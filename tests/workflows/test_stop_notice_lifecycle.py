"""Stop notices survive cancellation and durable inbox restoration."""

from __future__ import annotations

import asyncio
import copy

import pytest

from opencollab.domain.scheduler import SessionControlBlock
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler


def _seat(scheduler, session, aid):
    session.state.aid = aid
    session.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=aid, parent_aid=0, agent=session.agent, state=session.state)
    )
    scheduler._sessions[aid] = session


@pytest.mark.parametrize("locked_aid", [0, 2])
async def test_cancelling_notice_lock_wait_retains_each_uncommitted_sender(locked_aid):
    lead = ScriptedSession("lead", [])
    stopped = ScriptedSession("coder", [])
    other = ScriptedSession("reviewer", [])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, stopped, 1)
    _seat(scheduler, other, 2)
    stopped.state.cancel("budget exhausted")
    scheduler._unanswered[1] = {0: "lead-request", 2: "review-request"}
    blockers = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
    scheduler._tasks.update({0: blockers[0], 2: blockers[1]})
    lock = scheduler._locks.setdefault(locked_aid, asyncio.Lock())
    await lock.acquire()
    notification = asyncio.create_task(scheduler.notify_unanswered_senders(1, "budget exhausted"))
    try:
        await asyncio.sleep(0)
        notification.cancel()
        with pytest.raises(asyncio.CancelledError):
            await notification
        lock.release()

        expected = {0: "lead-request", 2: "review-request"} if locked_aid == 0 else {2: "review-request"}
        assert scheduler._unanswered.get(1) == expected

        await scheduler._drain_ready_message_inboxes()

        assert not scheduler._unanswered.get(1)
        assert len(scheduler._message_inbox[0]) == 1
        assert len(scheduler._message_inbox[2]) == 1
        assert scheduler._message_inbox[0][0].kind == "stop_notice"
        assert scheduler._message_inbox[2][0].kind == "stop_notice"
    finally:
        if lock.locked():
            lock.release()
        for blocker in blockers:
            blocker.cancel()
        await asyncio.gather(*blockers, return_exceptions=True)


async def test_restored_notice_keeps_its_envelope_and_is_delivered_once():
    lead = ScriptedSession("lead", [])
    stopped = ScriptedSession("coder", [])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, stopped, 1)
    stopped.state.cancel("budget exhausted")
    scheduler._unanswered[1] = {0: "request"}
    blocker = asyncio.create_task(asyncio.Event().wait())
    scheduler._tasks[0] = blocker
    try:
        await scheduler.notify_unanswered_senders(1, "budget exhausted")
        pending = copy.deepcopy(lead.state.pending_user_messages)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)

    restored_lead = ScriptedSession("lead", [])
    restored_lead.state.pending_user_messages = pending
    restored_coder = ScriptedSession("coder", [])
    restored, _ = build_scheduler(restored_lead, [])
    _seat(restored, restored_coder, 1)
    restored_coder.state.cancel("budget exhausted")
    tasks = []
    restored._start_agent_task = lambda aid, session: tasks.append(aid)

    await restored._drain_message_inbox(0)
    await restored._drain_message_inbox(0)

    assert tasks == [0]
    assert len(restored_lead.added) == 1
    assert restored_lead.added[0].startswith("<team-notice ")
    assert "budget exhausted" in restored_lead.added[0]
    assert restored_lead.state.pending_user_messages == []


async def test_concurrent_notifiers_queue_one_notice_after_the_target_lock_releases():
    lead = ScriptedSession("lead", [])
    stopped = ScriptedSession("coder", [])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, stopped, 1)
    stopped.state.cancel("budget exhausted")
    scheduler._unanswered[1] = {0: "request"}
    blocker = asyncio.create_task(asyncio.Event().wait())
    scheduler._tasks[0] = blocker
    lock = scheduler._locks.setdefault(0, asyncio.Lock())
    await lock.acquire()
    notifiers = [asyncio.create_task(scheduler.notify_unanswered_senders(1, "budget exhausted")) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(*notifiers)

        assert len(scheduler._message_inbox[0]) == 1
        assert len(lead.state.pending_user_messages) == 1
        assert not scheduler._unanswered.get(1)
    finally:
        if lock.locked():
            lock.release()
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
