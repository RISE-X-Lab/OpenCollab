"""Unit tests for the precheck guards and their wiring into the session run."""

from __future__ import annotations

import asyncio

import pytest
from session_run_loop_test_support import (
    FakeLLM,
    build_runner,
    collect_events,
    llm_response,
    run,
)

from opencollab.application.precheck_guards import (
    CancelGuard,
    LoopBlockGuard,
    SessionBudgetGuard,
    StepLimitGuard,
    TeamBudgetGuard,
    default_precheck_guards,
)
from opencollab.application.session_run import DEFAULT_LOOP_BLOCKED_LIMIT, ENFORCEMENT_ON
from opencollab.domain.precheck import PrecheckContext, StopDecision
from opencollab.domain.session import SessionPhase, SessionState, TurnEnforcementState


def _ctx(**overrides) -> PrecheckContext:
    fields = {
        "cancel_requested": False,
        "loop_blocked_since_progress": 0,
        "used_tokens": 0,
        "max_budget_tokens": 1_000,
        "team_budget_exhausted": False,
        "step_count": 0,
        "max_steps": 100,
    }
    fields.update(overrides)
    return PrecheckContext(**fields)


class _RecordingGuard:
    """A guard that remembers every context it saw and answers a fixed decision."""

    def __init__(self, decision: StopDecision | None = None):
        self.decision = decision
        self.seen: list[PrecheckContext] = []

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        self.seen.append(ctx)
        return self.decision


# --------------------------------------------------------------------------- #
# The guards on a bare context.
# --------------------------------------------------------------------------- #


def test_quiet_context_passes_every_built_in_guard():
    before, after = default_precheck_guards()
    assert [guard.check(_ctx()) for guard in before + after] == [None] * 5


@pytest.mark.parametrize(
    ("guard", "overrides", "decision"),
    [
        (
            CancelGuard(),
            {"cancel_requested": True},
            StopDecision("interrupted by user", message="[Session interrupted by user]"),
        ),
        (
            LoopBlockGuard(),
            {"loop_blocked_since_progress": DEFAULT_LOOP_BLOCKED_LIMIT},
            StopDecision(f"loop block limit reached: {DEFAULT_LOOP_BLOCKED_LIMIT} repeated tool calls"),
        ),
        (
            SessionBudgetGuard(),
            {"used_tokens": 1_000, "max_budget_tokens": 1_000},
            StopDecision("budget exceeded: 1000 tokens used"),
        ),
        (
            TeamBudgetGuard(),
            {"team_budget_exhausted": True},
            StopDecision("team budget exceeded: aggregate spend reached the global cap"),
        ),
        (
            StepLimitGuard(),
            {"step_count": 7, "max_steps": 7},
            StopDecision("step limit reached: 7 steps"),
        ),
    ],
)
def test_each_guard_stops_with_its_verbatim_reason(guard, overrides, decision):
    assert guard.check(_ctx(**overrides)) == decision


def test_thresholds_are_inclusive_and_one_below_passes():
    assert LoopBlockGuard(limit=2).check(_ctx(loop_blocked_since_progress=1)) is None
    assert LoopBlockGuard(limit=2).check(_ctx(loop_blocked_since_progress=2)) is not None
    assert SessionBudgetGuard().check(_ctx(used_tokens=999, max_budget_tokens=1_000)) is None
    assert StepLimitGuard().check(_ctx(step_count=6, max_steps=7)) is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_loop_block_guard_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError, match="positive integer"):
        LoopBlockGuard(limit=limit)


def test_built_in_order_matches_the_former_inline_precheck():
    before, after = default_precheck_guards()
    assert [type(guard) for guard in before] == [
        CancelGuard,
        LoopBlockGuard,
        SessionBudgetGuard,
        TeamBudgetGuard,
    ]
    assert [type(guard) for guard in after] == [StepLimitGuard]
    loop_guard = before[1]
    assert isinstance(loop_guard, LoopBlockGuard)
    assert loop_guard.limit == DEFAULT_LOOP_BLOCKED_LIMIT


# --------------------------------------------------------------------------- #
# Wiring into the session run.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cancelled", "loop_blocked", "used_tokens", "team_exhausted", "reason"),
    [
        (True, 3, 10, True, "interrupted by user"),
        (False, 3, 10, True, "loop block limit reached: 3 repeated tool calls"),
        (False, 0, 10, True, "budget exceeded: 10 tokens used"),
        (False, 0, 0, True, "team budget exceeded: aggregate spend reached the global cap"),
        (False, 0, 0, False, "step limit reached: 5 steps"),
    ],
)
def test_built_in_guards_stop_in_the_original_order(cancelled, loop_blocked, used_tokens, team_exhausted, reason):
    """Arm every trigger at once, then disarm them one by one: that walks the order."""
    events, bus = collect_events()
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        used_tokens=used_tokens,
        step_count=5,
        turn=TurnEnforcementState(loop_blocked_since_progress=loop_blocked),
    )
    llm = FakeLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        max_budget_tokens=10,
        max_steps=5,
        team_budget_exhausted=lambda: team_exhausted,
    )
    cancel_event = asyncio.Event()
    if cancelled:
        cancel_event.set()

    result = run(runner.run_loop(cancel_event=cancel_event))

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == reason
    assert events == [("error", {"reason": reason, "aid": -1})]


def test_injected_guards_run_in_order_and_the_first_decision_wins():
    first = _RecordingGuard()
    second = _RecordingGuard(StopDecision("custom stop", message="[custom notice]"))
    third = _RecordingGuard()
    fourth = _RecordingGuard()
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        precheck_guards=((first, second, third), (fourth,)),
    )

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "custom stop"
    assert state.messages[-1] == {"role": "system", "content": "[custom notice]"}
    assert events == [("error", {"reason": "custom stop", "aid": -1})]
    assert [len(guard.seen) for guard in (first, second, third, fourth)] == [1, 1, 0, 0]


def test_a_decision_without_message_gets_the_derived_notice():
    guard = _RecordingGuard(StopDecision("custom stop"))
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(state=state, llm=FakeLLM(), precheck_guards=((guard,), ()))

    run(runner.run_loop())

    assert state.messages[-1] == {"role": "system", "content": "[Custom stop. Session stopped.]"}


def test_guards_after_the_gate_run_when_the_gate_passes():
    before = _RecordingGuard()
    after = _RecordingGuard(StopDecision("late stop"))
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(state=state, llm=FakeLLM(), precheck_guards=((before,), (after,)))

    run(runner.run_loop())

    assert state.terminal_reason == "late stop"
    assert [len(before.seen), len(after.seen)] == [1, 1]


def test_guards_after_the_gate_are_skipped_when_the_gate_takes_the_turn():
    """The split exists for this: a gate that settles the turn ends the pass."""
    before = _RecordingGuard()
    after = _RecordingGuard(StopDecision("late stop"))
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        wind_down_done=True,
        wind_down_attempts=2,
    )
    llm = FakeLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        enforcement_strength=ENFORCEMENT_ON,
        precheck_guards=((before,), (after,)),
    )

    run(runner.run_loop())

    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "wind-down complete: forced commit within reserve"
    assert [len(before.seen), len(after.seen)] == [1, 0]


def test_context_snapshots_live_state_and_caps_each_pass():
    spy = _RecordingGuard()
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        used_tokens=42,
        step_count=3,
        turn=TurnEnforcementState(loop_blocked_since_progress=1),
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        llm=llm,
        max_budget_tokens=500,
        max_steps=9,
        team_budget_exhausted=lambda: False,
        precheck_guards=((spy,), ()),
    )

    run(runner.run_loop(cancel_event=asyncio.Event()))

    assert len(llm.calls) == 1
    assert spy.seen[0] == PrecheckContext(
        cancel_requested=False,
        loop_blocked_since_progress=1,
        used_tokens=42,
        max_budget_tokens=500,
        team_budget_exhausted=False,
        step_count=3,
        max_steps=9,
    )


def test_caps_changed_after_construction_reach_the_guards():
    """The scheduler's lease and the ``Session`` facade rewrite the caps mid-run."""
    spy = _RecordingGuard()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(
        state=state,
        llm=FakeLLM([llm_response(content="done")]),
        max_budget_tokens=500,
        max_steps=9,
        precheck_guards=((spy,), ()),
    )
    runner.max_budget_tokens = 700
    runner.max_steps = 4

    run(runner.run_loop())

    assert (spy.seen[0].max_budget_tokens, spy.seen[0].max_steps) == (700, 4)
