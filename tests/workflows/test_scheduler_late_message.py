"""Messages arriving during another inbox drain still reopen the recipient."""

from __future__ import annotations

import pytest

from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler, terminal


@pytest.mark.parametrize("ending", ["done", "stopped", "error", "exception", "diff_failure"])
async def test_message_arriving_during_terminal_inbox_drain_is_delivered(monkeypatch, ending):
    async def first_turn(session):
        if ending == "exception":
            raise RuntimeError("execution failed")
        session.state.set_phase(SessionPhase(ending if ending in {"stopped", "error"} else "done"))
        return "first answer"

    lead = ScriptedSession("lead", [first_turn, terminal("answered late message")])
    sender = ScriptedSession("sender", [])
    scheduler, _ = build_scheduler(lead, [])
    sender.state.aid = 1
    sender.scheduler = scheduler
    scheduler.table.add(SessionControlBlock(aid=1, parent_aid=0, agent=sender.agent, state=sender.state))
    scheduler._sessions[1] = sender
    sender.state.set_phase(SessionPhase.DONE)
    if ending == "diff_failure":
        lead.env = object()

        async def failing_diff(env, result, **_kwargs):
            del lead.env
            raise OSError("diff failed")

        monkeypatch.setattr(scheduler, "_append_worktree_diff", failing_diff)

    original_drain = scheduler._drain_ready_message_inboxes
    injected = False

    async def drain_with_late_message():
        nonlocal injected
        if not injected:
            injected = True
            ack = await scheduler.send_message(1, 0, "late", "please answer this message")
            assert ack.startswith("Message queued")
            assert scheduler._message_inbox[0]
        await original_drain()

    monkeypatch.setattr(scheduler, "_drain_ready_message_inboxes", drain_with_late_message)

    assert await scheduler.run("start") == "answered late message"
    assert "please answer this message" in lead.added[-1]
    assert not scheduler._message_inbox[0]
    assert lead.state.pending_user_messages == []
