"""Finishing a turn is how a sender waits for a teammate's answer.

``message_agent`` returns as soon as the message is queued, so a sender that
wants the answer has to do something in the meantime. The tool now tells it
that ending the turn is a real option; these tests are why that sentence is
true. A finished sender is reopened when the reply arrives, and the team turn
does not return while an inbox still holds a message.

The alternative -- keeping the sender alive on ``team_status`` polls until the
answer lands -- costs one full provider call per poll, which is the same
resource the run is measured in. That is worth pinning against regression: the
mechanism lives in two places that know nothing about each other (the phase FSM
allows DONE -> IDLE; the quiescence predicate refuses to call a team finished
while a message is undelivered), and either one alone would make the sentence
false.
"""

from __future__ import annotations

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    run,
    terminal,
)

from opencollab.adapters.tools.message import _DELIVERY_NOTE
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase


def _seat_coder(scheduler, coder: ScriptedSession, aid: int = 1) -> None:
    coder.state.aid = aid
    coder.scheduler = scheduler
    scheduler.table.add(
        SessionControlBlock(aid=aid, parent_aid=0, agent=coder.agent, state=coder.state)
    )
    scheduler._sessions[aid] = coder


def _hand_off_then_finish(step_result: str):
    async def step(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(0, 1, "implement", "please write the fix")
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": step_result})
        return step_result

    return step


def _reply_then_finish(sha: str):
    async def step(sess: ScriptedSession) -> str:
        await sess.scheduler.send_message(1, 0, "done", f"committed {sha}")
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": "sent the sha"})
        return "sent the sha"

    return step


def _land_what_arrived():
    async def step(sess: ScriptedSession) -> str:
        assert sess.added, "resumed without a teammate message"
        assert "6b5c529" in sess.added[-1]
        sess.state.set_phase(SessionPhase.DONE)
        answer = "landed 6b5c529"
        sess.state.append_message({"role": "assistant", "content": answer})
        return answer

    return step


def test_a_finished_sender_is_reopened_by_its_teammate_s_reply():
    lead = ScriptedSession("analyst", [_hand_off_then_finish("handed off"), _land_what_arrived()])
    coder = ScriptedSession("coder", [_reply_then_finish("6b5c529")])
    scheduler, _ = build_scheduler(lead, [])
    _seat_coder(scheduler, coder)

    answer = run(scheduler.run("fix the bug"))

    # The lead reached DONE once with nothing but a handoff to show, and the
    # reply put it back to work. Both halves matter: the answer is the one it
    # produced after the message, not the one it finished on.
    assert answer == "landed 6b5c529"
    assert lead._steps == []


def test_the_team_turn_does_not_end_while_a_message_is_undelivered():
    lead = ScriptedSession("analyst", [terminal("done on my own")])
    coder = ScriptedSession("coder", [])
    scheduler, _ = build_scheduler(lead, [])
    _seat_coder(scheduler, coder)
    lead.state.set_phase(SessionPhase.DONE)
    coder.state.set_phase(SessionPhase.DONE)

    assert scheduler._quiescent()

    scheduler._message_inbox.setdefault(1, []).append(object())

    assert not scheduler._quiescent()


@pytest.mark.parametrize("promise", ["reopens your turn", "finishing is how"])
def test_the_delivery_note_says_finishing_is_a_way_to_wait(promise: str):
    # A sender cannot discover this by trying: the failure mode of not knowing
    # it is burning the budget on polls, which looks like ordinary work.
    assert promise in _DELIVERY_NOTE
