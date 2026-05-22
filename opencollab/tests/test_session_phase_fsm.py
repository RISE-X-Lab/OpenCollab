from __future__ import annotations

import pytest

from opencollab.domain.session import (
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    InvalidPhaseTransition,
    SessionPhase,
    SessionState,
)


def state(phase: SessionPhase) -> SessionState:
    s = SessionState(messages=[])
    s.set_phase(phase)
    return s


def test_every_phase_is_in_the_transition_table():
    assert set(PHASE_TRANSITIONS) == set(SessionPhase)


def test_terminal_phases_only_resume_to_idle():
    for phase in TERMINAL_PHASES:
        assert PHASE_TRANSITIONS[phase] == frozenset({SessionPhase.IDLE})


@pytest.mark.parametrize(
    "src,dst",
    [(src, dst) for src, dsts in PHASE_TRANSITIONS.items() for dst in dsts],
)
def test_legal_edges_transition(src: SessionPhase, dst: SessionPhase):
    s = state(src)
    s.transition_to(dst)
    assert s.phase is dst


@pytest.mark.parametrize(
    "src,dst",
    [
        (SessionPhase.IDLE, SessionPhase.DONE),
        (SessionPhase.PRECHECK, SessionPhase.AUTOSAVING),
        (SessionPhase.CALLING_LLM, SessionPhase.DONE),
        (SessionPhase.HANDLING_RESPONSE, SessionPhase.PRECHECK),
        (SessionPhase.DONE, SessionPhase.PRECHECK),
        (SessionPhase.SCHEDULED, SessionPhase.CALLING_LLM),
    ],
)
def test_illegal_edges_raise(src: SessionPhase, dst: SessionPhase):
    s = state(src)
    with pytest.raises(InvalidPhaseTransition) as exc:
        s.transition_to(dst)
    assert exc.value.src is src
    assert exc.value.dst is dst
    assert s.phase is src  # phase unchanged on a rejected transition


def test_set_phase_is_unchecked_escape():
    # set_phase is the out-of-band primitive used for the ERROR escape and
    # process birth — it bypasses validation by design.
    s = state(SessionPhase.EXECUTING_TOOLS)
    s.set_phase(SessionPhase.ERROR)
    assert s.phase is SessionPhase.ERROR


def test_mark_done_and_clear_done_roundtrip():
    s = state(SessionPhase.HANDLING_RESPONSE)
    s.mark_done()
    assert s.phase is SessionPhase.DONE
    assert s.is_done is True
    s.clear_done()
    assert s.phase is SessionPhase.IDLE
    assert s.is_done is False


def test_clear_done_is_noop_when_not_done():
    s = state(SessionPhase.PRECHECK)
    s.clear_done()
    assert s.phase is SessionPhase.PRECHECK


def test_reset_for_user_turn_clears_done_and_hashes():
    s = state(SessionPhase.DONE)
    s.remember_tool_call_hash("h1")
    s.reset_for_user_turn()
    assert s.phase is SessionPhase.IDLE
    assert s.recent_call_hashes == []
