"""Targeted scheduler cancellation and descendant settlement."""

import asyncio

import pytest
from scheduler_awaiting_test_support import ScriptedSession, build_scheduler, run

from opencollab.application.scheduler import SchedulerTurnError
from opencollab.domain.pending import PendingRow, RowKind
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase


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
