"""Targeted scheduler cancellation and descendant settlement."""

import asyncio

import pytest

from opencollab.application.scheduler import SchedulerTurnError
from opencollab.domain.pending import PendingRow, RowKind
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler, run


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

def test_a_turn_offered_a_cancel_event_still_ends_when_the_team_goes_quiet():
    """The waiter is how a turn hears about cancellation, not work it waits on.

    Counting it as pending work means a turn that is offered a cancel event and
    never cancelled never returns: every agent is idle, the team is quiescent,
    and the loop is still waiting on an event nobody will set. The interactive
    CLI offers one on every turn so Ctrl+C has something to reach.
    """
    class AnsweringSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            self.state.set_phase(SessionPhase.DONE)
            self.state.append_message({"role": "assistant", "content": "the answer"})
            return "the answer"

    lead = AnsweringSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])

    async def scenario():
        answer = await asyncio.wait_for(
            scheduler.run_turn(0, "a question", cancel_event=asyncio.Event()),
            timeout=2,
        )
        assert answer == "the answer"

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


def test_cancel_settlement_reconciles_queued_inbox_and_notifies_unanswered_sender():
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
            sess.state.aid, "coder", "blocked child", tool_call_id="blocked-child"
        )
        sess.state.pending_events.add(
            PendingRow("blocked-child", RowKind.CHILD_AGENT, 0, child_aid)
        )
        sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
        return ""

    async def blocked_child(_sess):
        child_started.set()
        await release_child.wait()
        return "late child result"

    async def retry(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "retry"})
        return "retry"

    async def next_turn(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "next turn"})
        return "next turn"

    async def sibling_done(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "received stop notice"})
        return "received stop notice"

    lead = CancelAwareSession("lead", [suspend_on_child, retry, next_turn])
    child = ScriptedSession("coder", [blocked_child])
    sibling = CancelAwareSession("reviewer", [sibling_done])
    scheduler, _ = build_scheduler(lead, [child])
    sibling.state.aid = 2
    sibling.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=2, parent_aid=0, agent=sibling.agent, state=sibling.state)
    )
    scheduler._sessions[2] = sibling

    async def scenario():
        cancel_event = asyncio.Event()
        call = asyncio.create_task(scheduler.run("delegate", cancel_event=cancel_event))
        await asyncio.wait_for(child_started.wait(), 0.5)
        while lead.state.phase is not SessionPhase.AWAITING_EVENTS:
            await asyncio.sleep(0)
        await scheduler.send_message(2, 0, "reply", "accepted teammate content")
        cancel_event.set()

        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await asyncio.wait_for(call, 0.5)
        release_child.set()

        assert len(scheduler._message_inbox.get(0, [])) == 1
        assert len(lead.state.pending_user_messages) == 1
        assert lead.state.pending_user_messages[0]["message_content"] == "accepted teammate content"
        assert any("team-notice" in message for message in sibling.added)
        assert not scheduler._unanswered.get(0)
        assert scheduler._quiescent()
        assert await scheduler.run("new legitimate turn") == "next turn"
        assert any("accepted teammate content" in message for message in lead.added)
        assert lead.state.pending_user_messages == []

    run(scenario())


def test_cancelled_descendant_keeps_inbox_until_its_next_public_turn():
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
            sess.state.aid, "coder", "blocked child", tool_call_id="blocked-child"
        )
        sess.state.pending_events.add(
            PendingRow("blocked-child", RowKind.CHILD_AGENT, 0, child_aid)
        )
        sess.state.set_phase(SessionPhase.AWAITING_EVENTS)
        return ""

    async def blocked_child(_sess, _cancel_event):
        child_started.set()
        await release_child.wait()
        return "late child result"

    async def answer_queued_message(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "message delivered"})
        return "message delivered"

    async def answer_new_turn(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "child answer"})
        return "child answer"

    lead = CancelAwareSession("lead", [suspend_on_child])
    child = CancelAwareSession(
        "coder", [blocked_child, answer_queued_message, answer_new_turn]
    )
    sender = ScriptedSession("reviewer", [])
    scheduler, _ = build_scheduler(lead, [child])
    sender.state.aid = 2
    sender.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=2, parent_aid=0, agent=sender.agent, state=sender.state)
    )
    scheduler._sessions[2] = sender

    async def scenario():
        cancel_event = asyncio.Event()
        call = asyncio.create_task(scheduler.run("delegate", cancel_event=cancel_event))
        await asyncio.wait_for(child_started.wait(), 0.5)
        while lead.state.phase is not SessionPhase.AWAITING_EVENTS:
            await asyncio.sleep(0)
        await scheduler.send_message(2, 1, "late child work", "preserve child message")
        cancel_event.set()
        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await asyncio.wait_for(call, 0.5)

        assert child.state.phase is SessionPhase.STOPPED
        assert len(scheduler._message_inbox[1]) == 1
        assert len(child.state.pending_user_messages) == 1
        assert not scheduler._active_scheduler_tasks()
        assert scheduler._quiescent()

        assert await scheduler.run_turn(1, "new child turn") == "child answer"
        assert len(child.added) == 2
        assert sum("preserve child message" in message for message in child.added) == 1
        assert child.state.pending_user_messages == []
        assert scheduler._message_inbox.get(1, []) == []
        release_child.set()

    run(scenario())


def test_restored_cancelled_inbox_survives_unrelated_run_then_delivers_once():
    class AnsweringSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            step = self._steps.pop(0)
            return await step(self, cancel_event)

    async def answer_restored_message(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "handled restored message"})
        return "handled restored message"

    async def answer_new_turn(sess, _cancel_event):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "new lead answer"})
        return "new lead answer"

    async def answer_unrelated_run(sess):
        sess.state.mark_done()
        sess.state.append_message({"role": "assistant", "content": "unrelated answer"})
        return "unrelated answer"

    lead = AnsweringSession("lead", [answer_restored_message, answer_new_turn])
    lead.state.cancel("interrupted by user")
    lead.state.queue_pending_user_message(
        {
            "role": "user",
            "content": (
                '<teammate-message teammate_id="A1" summary="late" '
                'message_id="restored-late-id">\nrestored teammate content\n'
                "</teammate-message>"
            ),
            "message_content": "restored teammate content",
            "from_aid": 1,
            "to_aid": 0,
            "from_role": "reviewer",
            "to_role": "lead",
            "summary": "late",
            "message_id": "restored-late-id",
            "delivery_status": "pending",
            "timestamp": "restored-time",
        }
    )
    scheduler, _ = build_scheduler(lead, [])
    sender = ScriptedSession("reviewer", [answer_unrelated_run])
    sender.state.aid = 1
    sender.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=sender.agent, state=sender.state)
    )
    scheduler._sessions[1] = sender

    async def scenario():
        assert scheduler._message_inbox[0]
        assert 0 in scheduler._cancelled_turn_inbox_holds
        assert await scheduler.run_turn(1, "unrelated run") == "unrelated answer"
        assert len(scheduler._message_inbox[0]) == 1
        assert scheduler._quiescent()

        assert await scheduler.run_turn(0, "new lead turn") == "new lead answer"
        assert sum("restored teammate content" in message for message in lead.added) == 1
        assert lead.state.pending_user_messages == []
        assert scheduler._message_inbox.get(0, []) == []
        assert 0 not in scheduler._cancelled_turn_inbox_holds

    run(scenario())


def test_cancel_event_during_restored_awaiting_preamble_settles_without_appending_user_turn():
    child_started = asyncio.Event()
    parent_started = asyncio.Event()
    release_child = asyncio.Event()

    class CancelAwareSession(ScriptedSession):
        async def add_user_message(self, content: str) -> None:
            await super().add_user_message(content)
            self.state.reset_for_user_turn()

        async def run_loop(self, cancel_event=None) -> str:
            step = self._steps.pop(0)
            return await step(self, cancel_event)

    async def resumed_old_turn(sess, _cancel_event):
        # Recovery stays suspended while its prior child is outstanding.
        assert sess.state.phase is SessionPhase.AWAITING_EVENTS
        assert not sess.state.pending_events.is_complete()
        parent_started.set()
        return ""

    async def blocked_child(_sess):
        child_started.set()
        await release_child.wait()
        return "late child result"

    lead = CancelAwareSession("lead", [resumed_old_turn])
    child = ScriptedSession("coder", [blocked_child])
    scheduler, _ = build_scheduler(lead, [child])

    async def scenario():
        child_aid = await scheduler.spawn(0, "coder", "blocked child", tool_call_id="blocked-child")
        lead.state.pending_events.add(
            PendingRow("blocked-child", RowKind.CHILD_AGENT, 0, child_aid)
        )
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        await asyncio.wait_for(child_started.wait(), 0.5)
        cancel_event = asyncio.Event()
        call = asyncio.create_task(scheduler.run("must not append", cancel_event=cancel_event))
        await asyncio.wait_for(parent_started.wait(), 0.5)
        cancel_event.set()
        with pytest.raises(SchedulerTurnError, match="interrupted by user"):
            await asyncio.wait_for(call, 0.5)
        assert "must not append" not in lead.added
        assert lead.state.phase is SessionPhase.STOPPED
        assert child.state.phase is SessionPhase.STOPPED
        assert not scheduler._active_scheduler_tasks()
        assert not scheduler._turn_lease
        assert scheduler._quiescent()
        release_child.set()

    run(scenario())


@pytest.mark.parametrize(
    ("terminal_phase", "terminal_reason"),
    [
        (SessionPhase.DONE, None),
        (SessionPhase.ERROR, "sticky prior error"),
    ],
)
def test_cancel_and_prior_terminal_completion_together_keep_main_recovery_outcome(
    terminal_phase, terminal_reason
):
    started = asyncio.Event()
    finish = asyncio.Event()
    cancel_event = asyncio.Event()

    class CompletingSession(ScriptedSession):
        async def run_loop(self, _cancel_event=None):
            self.state.resume_to_idle()
            self.state.set_phase(SessionPhase.PRECHECK)
            started.set()
            await finish.wait()
            self.state.append_message({"role": "assistant", "content": "prior result"})
            self.state.set_phase(terminal_phase)
            self.state.terminal_reason = terminal_reason
            return "prior result"

    lead = CompletingSession("lead", [])
    scheduler, _ = build_scheduler(lead, [])
    lead.state.set_phase(SessionPhase.AWAITING_EVENTS)

    async def scenario():
        call = asyncio.create_task(
            scheduler.run("incoming request", cancel_event=cancel_event)
        )
        await asyncio.wait_for(started.wait(), 0.5)
        cancel_event.set()
        finish.set()

        with pytest.raises(SchedulerTurnError) as error:
            await asyncio.wait_for(call, 0.5)

        expected_phase = SessionPhase.STOPPED if terminal_phase is SessionPhase.DONE else terminal_phase
        expected_reason = "interrupted by user" if terminal_phase is SessionPhase.DONE else terminal_reason
        assert error.value.phase is expected_phase
        assert error.value.partial_answer == "prior result"
        assert lead.state.phase is expected_phase
        assert lead.state.terminal_reason == expected_reason
        assert lead.state.messages == [{"role": "assistant", "content": "prior result"}]
        assert scheduler.table.get(0).result == (
            "prior result" if terminal_phase is SessionPhase.DONE else "Error: agent failed: sticky prior error"
        )
        assert lead.added == []
        assert not scheduler._active_scheduler_tasks()
        assert not scheduler._turn_lease

    run(scenario())
