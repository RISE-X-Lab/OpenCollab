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


def test_phase_and_terminal_sets_are_exactly_as_declared():
    # Value-lock backstopping the self-referential topology tests in this file:
    # they derive their parametrize cases FROM PHASE_TRANSITIONS/TERMINAL_PHASES,
    # so a wrong topology would still satisfy them. This pins the exact intended
    # shape as string literals, so adding/removing a phase or terminal is a
    # conscious, reviewed edit here. Ten phases; three terminals (DONE / STOPPED /
    # ERROR) — every controlled halt is STOPPED(reason=...), not its own member.
    assert {p.value for p in SessionPhase} == {
        "idle",
        "precheck",
        "calling_llm",
        "handling_response",
        "executing_tools",
        "awaiting_events",
        "autosaving",
        "done",
        "stopped",
        "error",
    }
    assert {p.value for p in TERMINAL_PHASES} == {"done", "stopped", "error"}


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
        (SessionPhase.STOPPED, SessionPhase.PRECHECK),
        (SessionPhase.AWAITING_EVENTS, SessionPhase.DONE),
        (SessionPhase.AWAITING_EVENTS, SessionPhase.AUTOSAVING),
        (SessionPhase.EXECUTING_TOOLS, SessionPhase.PRECHECK),
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
    # set_phase is the out-of-band primitive used for process birth and
    # snapshot/restore — it bypasses validation by design (EXECUTING_TOOLS ->
    # STOPPED is not a legal run-loop edge).
    s = state(SessionPhase.EXECUTING_TOOLS)
    s.set_phase(SessionPhase.STOPPED)
    assert s.phase is SessionPhase.STOPPED


@pytest.mark.parametrize("src", list(SessionPhase))
def test_fail_escapes_to_error_from_any_phase(src: SessionPhase):
    s = state(src)
    s.fail()
    assert s.phase is SessionPhase.ERROR


@pytest.mark.parametrize("src", list(SessionPhase))
def test_cancel_escapes_to_stopped_from_any_phase(src: SessionPhase):
    s = state(src)
    s.cancel()
    assert s.phase is SessionPhase.STOPPED


@pytest.mark.parametrize("src", sorted(TERMINAL_PHASES, key=lambda p: p.value))
def test_resume_to_idle_from_each_terminal(src: SessionPhase):
    s = state(src)
    s.resume_to_idle()
    assert s.phase is SessionPhase.IDLE


@pytest.mark.parametrize(
    "src", [p for p in SessionPhase if p not in TERMINAL_PHASES]
)
def test_resume_to_idle_is_noop_when_not_terminal(src: SessionPhase):
    s = state(src)
    s.resume_to_idle()
    assert s.phase is src


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


def test_awaiting_events_is_non_terminal_and_resumes_to_precheck():
    assert SessionPhase.AWAITING_EVENTS not in TERMINAL_PHASES
    s = state(SessionPhase.EXECUTING_TOOLS)
    s.transition_to(SessionPhase.AWAITING_EVENTS)
    s.transition_to(SessionPhase.PRECHECK)
    assert s.phase is SessionPhase.PRECHECK


def test_reset_for_user_turn_clears_done_and_hashes():
    s = state(SessionPhase.DONE)
    s.remember_tool_call_hash("h1")
    s.reset_for_user_turn()
    assert s.phase is SessionPhase.IDLE
    assert s.recent_call_hashes == []


def test_reset_for_user_turn_preserves_step_count():
    # step_count is a session-lifetime counter, not per-turn — see
    # reset_for_user_turn. A multi-turn session keeps accumulating.
    s = state(SessionPhase.DONE)
    s.set_step_count(7)
    s.reset_for_user_turn()
    assert s.step_count == 7


def test_fail_and_cancel_record_terminal_reason():
    s = state(SessionPhase.CALLING_LLM)
    s.fail(reason="ValueError: boom")
    assert s.phase is SessionPhase.ERROR
    assert s.terminal_reason == "ValueError: boom"

    s2 = state(SessionPhase.EXECUTING_TOOLS)
    s2.cancel()
    assert s2.phase is SessionPhase.STOPPED
    assert s2.terminal_reason == "cancelled"


def test_transition_to_records_terminal_reason_and_resume_clears_it():
    s = state(SessionPhase.PRECHECK)
    s.transition_to(SessionPhase.STOPPED, reason="step limit reached: 5 steps")
    assert s.phase is SessionPhase.STOPPED
    assert s.terminal_reason == "step limit reached: 5 steps"
    s.resume_to_idle()
    assert s.phase is SessionPhase.IDLE
    assert s.terminal_reason is None
