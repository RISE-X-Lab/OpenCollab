"""Bounded failure notices preserve a recovery route when delivery is deferred."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

from opencollab.application._scheduler_constants import MAX_TEAMMATE_INBOX_MESSAGES, MAX_TEAMMATE_MESSAGE_BYTES
from opencollab.domain.scheduler import SessionControlBlock
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler


class RetainedEnvironment:
    def __init__(self):
        self.recovery_location = None

    def retain_changes(self):
        self.recovery_location = "/tmp/retained-worktree"
        return self.recovery_location


def _stopped_sender(scheduler):
    session = ScriptedSession("coder", [])
    session.state.aid = 1
    session.env = RetainedEnvironment()
    session.state.cancel("provider unavailable")
    scheduler.table.add(SessionControlBlock(aid=1, parent_aid=0, agent=session.agent, state=session.state))
    scheduler._sessions[1] = session
    scheduler._unanswered[1] = {0: "request-1"}
    return session


@pytest.mark.parametrize("partial", ["a" * 10000, "\u4e2d\u6587" * 2000, "<&>" * 1500])
async def test_notice_retains_work_when_xml_or_utf8_exceeds_message_limit(partial):
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    stopped = _stopped_sender(scheduler)
    blocker = asyncio.create_task(asyncio.Event().wait())
    scheduler._tasks[0] = blocker
    try:
        await scheduler.notify_unanswered_senders(1, "provider unavailable", partial_result=partial)
        rows = lead.state.pending_user_messages
        assert len(rows) == 1
        xml = rows[0]["content"]
        assert len(xml.encode()) <= MAX_TEAMMATE_MESSAGE_BYTES
        assert stopped.env.recovery_location in ET.fromstring(xml).text
        assert not scheduler._unanswered.get(1)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)


async def test_full_inbox_retains_work_and_retries_with_recovery_location():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    stopped = _stopped_sender(scheduler)
    blocker = asyncio.create_task(asyncio.Event().wait())
    scheduler._tasks[0] = blocker
    # Count saturation is checked before inspecting queued message bodies.
    scheduler._message_inbox[0] = [None] * MAX_TEAMMATE_INBOX_MESSAGES
    try:
        await scheduler.notify_unanswered_senders(1, "provider unavailable", partial_result="partial patch")
        assert stopped.env.recovery_location == "/tmp/retained-worktree"
        assert scheduler._unanswered[1] == {0: "request-1"}
        scheduler._message_inbox[0].clear()
        await scheduler.notify_unanswered_senders(1, "provider unavailable")
        assert len(lead.state.pending_user_messages) == 1
        assert stopped.env.recovery_location in lead.state.pending_user_messages[0]["content"]
        assert not scheduler._unanswered.get(1)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
