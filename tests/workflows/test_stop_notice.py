"""A seat that stops without answering tells whoever is waiting on it.

``message_agent`` promises the sender that an answer, if one comes, reopens its
turn, and tells it that finishing is how to wait. A teammate that is stopped --
its token budget spent, its environment revoked -- can never send that answer,
and a prebuilt teammate has no pending row for ``_deliver_to_parent`` to fill,
so nothing reached the sender at all. The run then ended the moment the stopped
seat did, with the sender still waiting.

09-22 on the s2dual cells this was most of what the delegation readouts
recorded: in 21 runs the Adopter briefed Coder A, ended its turn to wait as the
tool told it to, and Coder A was stopped at its 2M cap before replying. The run
ended there, Coder B was never briefed, nothing reached the graded tree, and
the run read as "only one Coder was used". A notice is the fact the sender was
missing -- that seat has stopped and will not answer -- and nothing else: it
names the seat and the reason and does not say what to do about it.

Scope, each pinned below: only a stop the seat did not choose (STOPPED or
ERROR) sends one; a seat that answered first, or that finished on its own
without answering, sends none; and a sender that is itself stopped is not
reopened for it, because it could not act on it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.scheduler_types import (
    QueuedTeammateMessage,
    SchedulerTurnError,
)
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase
from opencollab.domain.team import Topology
from tests.support.scheduler_awaiting_test_support import (
    ScriptedFactory,
    ScriptedSession,
    build_scheduler,
    run,
)

BUDGET_STOP = (
    "budget exhausted before model call: conservative input reservation "
    "requires 72741 of 21505 remaining tokens, leaving no output headroom"
)


def _seat(scheduler, session: ScriptedSession, aid: int) -> None:
    session.state.aid = aid
    session.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=aid, parent_aid=0, agent=session.agent, state=session.state)
    )
    scheduler._sessions[aid] = session


def _hand_off_then_finish(to_aid: int = 1):
    async def step(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(0, to_aid, "brief", "please write the fix")
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "handed off"})
        return "handed off"

    return step


def _hand_off_then_stop():
    async def step(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(0, 1, "brief", "please write the fix")
        sess.state.set_phase(SessionPhase.STOPPED)
        sess.state.terminal_reason = BUDGET_STOP
        return ""

    return step


def _stopped(reason: str = BUDGET_STOP):
    async def step(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.STOPPED)
        sess.state.terminal_reason = reason
        return ""

    return step


def _reply_then_stop():
    async def step(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(1, 0, "done", "committed 6b5c529")
        sess.state.set_phase(SessionPhase.STOPPED)
        sess.state.terminal_reason = BUDGET_STOP
        return ""

    return step


def _finish_without_answering():
    async def step(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "did it"})
        return "did it"

    return step


def _finish(answer: str):
    async def step(sess: ScriptedSession) -> str:
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": answer})
        return answer

    return step


@pytest.mark.parametrize("ending", ["stopped", "error", "exception", "cancelled"])
def test_a_seat_stopped_before_answering_reopens_the_waiting_sender(ending):
    async def fail_before_answering(session):
        if ending == "exception":
            raise RuntimeError("environment revoked")
        if ending == "cancelled":
            raise asyncio.CancelledError
        if ending == "error":
            session.state.fail("environment revoked")
        else:
            session.state.cancel(BUDGET_STOP)
        return ""

    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _finish("saw the stop")])
    coder = ScriptedSession("coder_a", [fail_before_answering])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    answer = run(scheduler.run("fix the bug"))

    # Reopened once, by the notice: its answer is the one given after it.
    assert answer == "saw the stop"
    assert lead._steps == []
    notice = lead.added[-1]
    assert "coder_a" in notice
    assert "will not answer" in notice
    expected_reason = (
        BUDGET_STOP if ending == "stopped" else "cancelled" if ending == "cancelled" else "environment revoked"
    )
    assert expected_reason in notice
    # Not dressed as the coder's own words: the coder wrote nothing.
    assert "<teammate-message" not in notice


def test_a_seat_that_answered_before_it_stopped_sends_no_notice():
    def _land():
        async def step(sess: ScriptedSession) -> str:
            assert "6b5c529" in sess.added[-1]
            sess.state.set_phase(SessionPhase.DONE)
            sess.state.append_message({"role": "assistant", "content": "landed"})
            return "landed"

        return step

    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _land()])
    coder = ScriptedSession("coder_a", [_reply_then_stop()])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    # A notice here would reopen the lead a third time, with no step left.
    assert run(scheduler.run("fix the bug")) == "landed"
    assert not any("will not answer" in item for item in lead.added)


def test_a_seat_that_finishes_on_its_own_without_answering_sends_no_notice():
    lead = ScriptedSession("adopter", [_hand_off_then_finish()])
    coder = ScriptedSession("coder_a", [_finish_without_answering()])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    assert run(scheduler.run("fix the bug")) == "handed off"
    assert lead.added == ["fix the bug"]


def test_a_sender_that_has_itself_stopped_is_not_reopened():
    lead = ScriptedSession("adopter", [_hand_off_then_stop()])
    coder = ScriptedSession("coder_a", [_stopped()])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    with pytest.raises(SchedulerTurnError):
        run(scheduler.run("fix the bug"))
    assert lead.added == ["fix the bug"]


def test_a_notice_batched_with_a_teammate_message_keeps_its_own_envelope():
    notice = QueuedTeammateMessage(
        from_aid=1,
        to_aid=0,
        summary="coder_a stopped",
        content="coder_a (A1) has stopped and will not answer.",
        xml='<team-notice about="A1">\ncoder_a (A1) has stopped and will not answer.\n</team-notice>',
        kind="stop_notice",
    )
    reply = QueuedTeammateMessage(
        from_aid=2, to_aid=0, summary="done", content="committed 6b5c529", xml="<x/>"
    )

    batch = Scheduler._format_teammate_message_batch([notice, reply])

    assert notice.xml in batch
    assert batch.count("<teammate-message ") == 1


def test_the_notice_is_its_own_trace_row_and_walks_no_edge(tmp_path):
    tracer = Tracer(run_id="stop-notice", output_dir=str(tmp_path))
    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _finish("saw the stop")])
    coder = ScriptedSession("coder_a", [_stopped()])

    async def sink(event):
        return None

    scheduler = Scheduler(
        session_factory=ScriptedFactory([], {}),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        tracer=tracer,
    )
    lead.scheduler = scheduler
    scheduler.register_lead(lead)
    _seat(scheduler, coder, 1)

    try:
        assert run(scheduler.run("fix the bug")) == "saw the stop"
    finally:
        tracer.flush()
        tracer.close()

    with open(tracer.path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    notices = [row["payload"] for row in rows if row["type"] == "stop_notice"]
    sent = [row["payload"] for row in rows if row["type"] == "message_sent"]

    assert len(notices) == 1
    assert notices[0]["aid"] == 1
    assert notices[0]["role"] == "coder_a"
    assert notices[0]["to_aid"] == 0
    assert notices[0]["to_role"] == "adopter"
    assert notices[0]["reason"] == BUDGET_STOP
    assert notices[0]["unanswered_message_id"] == sent[0]["message_id"]
    # Only the lead's own brief crossed an edge; the notice is not traffic.
    assert [(row["from_aid"], row["to_aid"]) for row in sent] == [(0, 1)]


def test_one_stop_sends_one_notice_and_spends_the_record():
    lead = ScriptedSession("adopter", [_hand_off_then_finish(1), _finish("first")])
    coder = ScriptedSession("coder_a", [_stopped()])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    run(scheduler.run("fix the bug"))

    # The unanswered record is spent by the notice: one stop, one notice.
    assert sum("will not answer" in item for item in lead.added) == 1
    assert scheduler._unanswered.get(1, {}) == {}
    run(scheduler.notify_unanswered_senders(1, "stopped again"))
    assert sum("will not answer" in item for item in lead.added) == 1


def test_a_seat_that_only_answered_is_not_told_when_the_other_stops():
    def _reply_then_finish():
        async def step(sess: ScriptedSession) -> str:
            await sess.scheduler.send_message(1, 0, "done", "committed 6b5c529")
            sess.state.set_phase(SessionPhase.DONE)
            sess.state.append_message({"role": "assistant", "content": "sent the sha"})
            return "sent the sha"

        return step

    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _stopped()])
    coder = ScriptedSession("coder_a", [_reply_then_finish()])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    # The lead stops holding the coder's answer unanswered. The coder handed
    # nothing over, so it is waiting on nothing; reopening it would only spend
    # its budget, and it has no step left to spend it on.
    with pytest.raises(SchedulerTurnError):
        run(scheduler.run("fix the bug"))
    assert len(coder.added) == 1
    assert not any("will not answer" in item for item in coder.added)


def test_a_new_request_after_a_reply_can_reverse_the_waiting_direction():
    async def reply_and_request(session):
        await session.scheduler.send_message(1, 0, "answer", "committed 6b5c529")
        await session.scheduler.send_message(1, 0, "request", "please review my next change")
        session.state.mark_done()
        return "waiting on review"

    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _stopped("review budget exhausted")])
    coder = ScriptedSession("coder_a", [reply_and_request, _finish("saw reviewer stop")])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    with pytest.raises(SchedulerTurnError):
        run(scheduler.run("fix the bug"))

    assert not coder._steps
    assert "review budget exhausted" in coder.added[-1]
    assert sum("will not answer" in item for item in coder.added) == 1


def test_notice_route_preserves_roster_identity_checks_and_regular_topology():
    lead = ScriptedSession("adopter", [])
    coder = ScriptedSession("coder_a", [])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)
    coder.state.cancel("budget exhausted")
    scheduler._topology = Topology(edges={"adopter": frozenset({"coder_a"}), "coder_a": frozenset()})
    notice = QueuedTeammateMessage(
        from_aid=1, to_aid=0, summary="stopped", content="stopped",
        xml='<team-notice about="A1">stopped</team-notice>',
        from_role="coder_a", to_role="adopter", kind="stop_notice", restored=True,
    )

    assert scheduler._message_route_error(0, notice) is None
    assert scheduler._message_route_error(0, replace(notice, kind="teammate"))[0] == "restored_topology_forbidden"
    assert (
        scheduler._message_route_error(0, replace(notice, to_role="somebody_else"))[0] == "restored_target_role_changed"
    )
    assert (
        scheduler._message_route_error(0, replace(notice, from_role="somebody_else"))[0]
        == "restored_sender_role_changed"
    )
    assert scheduler._message_route_error(0, replace(notice, from_aid=7))[0] == "restored_sender_gone"
    assert scheduler._message_route_error(0, replace(notice, xml="<teammate-message/>"))[0] == "restored_notice_invalid"
    coder.state.resume_to_idle()
    assert scheduler._message_route_error(0, notice)[0] == "restored_notice_sender_active"


@pytest.mark.parametrize(
    ("role", "reason"),
    [("coder_a", "<&>" * 20_000), ("编" * 42 + "ab", "<中😀&>" * 20_000)],
)
def test_long_stop_reason_fits_the_existing_delivery_bounds(role, reason):
    from opencollab.application._scheduler_constants import MAX_TEAMMATE_MESSAGE_BYTES

    lead = ScriptedSession("adopter", [_hand_off_then_finish(), _finish("saw the stop")])
    coder = ScriptedSession(role, [_stopped(reason)])
    scheduler, _ = build_scheduler(lead, [])
    _seat(scheduler, coder, 1)

    assert run(scheduler.run("fix the bug")) == "saw the stop"
    assert len(lead.added[-1].encode("utf-8")) <= MAX_TEAMMATE_MESSAGE_BYTES


def test_notice_retries_after_inbox_backpressure_without_exceeding_bounds():
    from opencollab.application._scheduler_constants import MAX_TEAMMATE_INBOX_MESSAGES

    async def scenario():
        lead = ScriptedSession("adopter", [])
        coder = ScriptedSession("coder_a", [])
        scheduler, _ = build_scheduler(lead, [])
        _seat(scheduler, coder, 1)
        coder.state.cancel("budget exhausted")
        scheduler._unanswered[1] = {0: "request-message"}
        blocker = asyncio.create_task(asyncio.Event().wait())
        scheduler._tasks[0] = blocker
        scheduler._message_inbox[0] = [
            QueuedTeammateMessage(from_aid=1, to_aid=0, summary="old", content="old", xml="old")
            for _ in range(MAX_TEAMMATE_INBOX_MESSAGES)
        ]
        try:
            await scheduler.notify_unanswered_senders(1, "budget exhausted")
            assert len(scheduler._message_inbox[0]) == MAX_TEAMMATE_INBOX_MESSAGES
            assert scheduler._unanswered[1] == {0: "request-message"}
            scheduler._message_inbox[0].clear()
            await scheduler._drain_ready_message_inboxes()
            assert len(scheduler._message_inbox[0]) == 1
            assert scheduler._message_inbox[0][0].kind == "stop_notice"
            assert not scheduler._unanswered.get(1)
        finally:
            blocker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocker

    run(scenario())


def test_a_request_rejected_on_delivery_does_not_create_a_stop_notice():
    async def scenario():
        lead = ScriptedSession("adopter", [])
        coder = ScriptedSession("coder_a", [])
        scheduler, _ = build_scheduler(lead, [])
        _seat(scheduler, coder, 1)
        scheduler._topology = Topology(edges={"adopter": frozenset(), "coder_a": frozenset()})
        request = QueuedTeammateMessage(
            from_aid=0, to_aid=1, summary="request", content="request",
            xml="<teammate-message/>", from_role="adopter", to_role="coder_a", message_id="request-id",
        )
        scheduler._message_inbox[1] = [request]
        scheduler._record_teammate_exchange(0, 1, "request-id")
        await scheduler._drain_message_inbox(1)
        coder.state.cancel("budget exhausted")
        await scheduler.notify_unanswered_senders(1, "budget exhausted")
        assert lead.added == []
        assert not scheduler._unanswered.get(1)

    run(scenario())


def test_invalid_oversized_role_cannot_block_notice_construction(caplog):
    async def scenario():
        lead = ScriptedSession("adopter", [])
        coder = ScriptedSession("x" * 20_000, [])
        scheduler, _ = build_scheduler(lead, [])
        _seat(scheduler, coder, 1)
        coder.state.cancel("budget exhausted")
        scheduler._unanswered[1] = {0: "request-id"}

        await asyncio.wait_for(scheduler.notify_unanswered_senders(1, "budget exhausted"), timeout=1)

        assert lead.added == []
        assert not scheduler._message_inbox.get(0)

    run(scenario())
    assert "stop notice role is invalid" in caplog.text
