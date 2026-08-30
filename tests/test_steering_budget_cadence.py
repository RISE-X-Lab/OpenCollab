"""Cadence of the budget status line: every-step / thresholds / off.

The knob changes only *how often* the ``[Budget: ...]`` line rides along; the
reads-without-write rungs (soft advice, hard ``tool_choice`` force) are the same
in all three modes. The default must stay byte-identical to the pre-knob
behaviour, so an unconfigured run is unchanged.
"""

from __future__ import annotations

from session_run_loop_test_support import (
    FakeLLM,
    _agent_with_tools,
    build_runner,
    llm_response,
    run,
)

from opencollab.application.steering import (
    BUDGET_NUDGE_ENV_VAR,
    BUDGET_NUDGE_EVERY_STEP,
    BUDGET_NUDGE_OFF,
    BUDGET_NUDGE_THRESHOLDS,
    READS_NUDGE_HARD,
    build_steering_block,
    resolve_budget_nudge_mode,
)
from opencollab.domain.session import SessionState, TurnEnforcementState

BUDGET = 100_000


def _block(used, *, mode=None, prev=0, reads=0):
    kwargs = dict(
        used_tokens=used,
        max_budget_tokens=BUDGET,
        step_count=3,
        max_steps=40,
        reads=reads,
        has_write=True,
        has_structured_output=False,
        structured_override=None,
        prev_used_tokens=prev,
    )
    if mode is not None:
        kwargs["budget_nudge_mode"] = mode
    return build_steering_block(**kwargs)


# --------------------------------------------------------------------------
# mode resolution
# --------------------------------------------------------------------------

def test_unset_or_unknown_env_resolves_to_every_step():
    # Unset, blank and garbage all fall back to the pre-knob behaviour, so a run
    # that does not opt in is not silently switched to a different cadence.
    assert resolve_budget_nudge_mode({}) == BUDGET_NUDGE_EVERY_STEP
    assert resolve_budget_nudge_mode({BUDGET_NUDGE_ENV_VAR: ""}) == BUDGET_NUDGE_EVERY_STEP
    assert resolve_budget_nudge_mode({BUDGET_NUDGE_ENV_VAR: "sometimes"}) == BUDGET_NUDGE_EVERY_STEP

def test_env_selects_each_of_the_three_modes_case_insensitively():
    assert resolve_budget_nudge_mode({BUDGET_NUDGE_ENV_VAR: "every-step"}) == BUDGET_NUDGE_EVERY_STEP
    assert resolve_budget_nudge_mode({BUDGET_NUDGE_ENV_VAR: " Thresholds "}) == BUDGET_NUDGE_THRESHOLDS
    assert resolve_budget_nudge_mode({BUDGET_NUDGE_ENV_VAR: "OFF"}) == BUDGET_NUDGE_OFF

def test_process_env_is_the_default_source(monkeypatch):
    monkeypatch.setenv(BUDGET_NUDGE_ENV_VAR, "off")
    assert resolve_budget_nudge_mode() == BUDGET_NUDGE_OFF
    monkeypatch.delenv(BUDGET_NUDGE_ENV_VAR)
    assert resolve_budget_nudge_mode() == BUDGET_NUDGE_EVERY_STEP


# --------------------------------------------------------------------------
# every-step (the default) is unchanged
# --------------------------------------------------------------------------

def test_default_argument_is_byte_identical_to_explicit_every_step():
    # The knob's default value must reproduce the old message exactly, otherwise
    # every historical run becomes incomparable with a new one.
    default_msg, default_override, default_level = _block(50_000)
    explicit_msg, explicit_override, explicit_level = _block(50_000, mode=BUDGET_NUDGE_EVERY_STEP)
    assert default_msg == explicit_msg == {
        "role": "user",
        "content": "[Budget: ~50k/100k tokens left, ~37 steps left.]",
    }
    assert (default_override, default_level) == (explicit_override, explicit_level)

def test_every_step_emits_the_line_even_without_a_crossing():
    # prev == used: nothing crossed, yet every-step still speaks.
    msg, _override, _level = _block(50_000, mode=BUDGET_NUDGE_EVERY_STEP, prev=50_000)
    assert msg is not None and "tokens left" in msg["content"]

def test_omitting_the_mode_speaks_on_a_turn_that_crosses_nothing():
    # The distinguishing case for the DEFAULT value: with prev == used a
    # thresholds default would stay silent here, so this pins the default to
    # every-step rather than merely to "some mode that happens to speak".
    msg, _override, _level = _block(50_000, prev=50_000)
    assert msg is not None and "tokens left" in msg["content"]


# --------------------------------------------------------------------------
# off
# --------------------------------------------------------------------------

def test_off_returns_no_message_at_all_when_no_nudge_is_due():
    msg, override, level = _block(50_000, mode=BUDGET_NUDGE_OFF)
    assert msg is None
    assert override is None and level is None

def test_off_still_delivers_the_hard_write_nudge_without_a_budget_line():
    # The read-loop brake is NOT part of this knob: at the hard rung the demand
    # and the tool_choice force survive, only the budget line is gone.
    msg, override, level = _block(50_000, mode=BUDGET_NUDGE_OFF, reads=READS_NUDGE_HARD)
    assert override == "required" and level == "hard"
    assert "Budget:" not in msg["content"]
    assert msg["content"].startswith("You have read")


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

def test_thresholds_is_silent_before_the_first_band():
    msg, _override, _level = _block(19_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=10_000)
    assert msg is None

def test_thresholds_fires_on_the_turn_that_steps_over_a_band():
    msg, _override, _level = _block(21_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=19_000)
    assert msg is not None and "79k/100k tokens left" in msg["content"]

def test_thresholds_fires_exactly_at_the_band_boundary():
    # 20% used lands ON the band: at-or-above counts as crossed.
    msg, _override, _level = _block(20_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=19_999)
    assert msg is not None

def test_thresholds_does_not_repeat_a_band_already_crossed():
    # Spend keeps climbing inside the 20-40% band: no second reminder.
    msg, _override, _level = _block(30_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=21_000)
    assert msg is None

def test_thresholds_speaks_exactly_four_times_over_a_monotone_walk():
    # A run that spends 4k per step from 0 to 100% must hear the line on the four
    # crossing steps and on nothing else.
    prev = 0
    spoke_at = []
    for used in range(4_000, 100_001, 4_000):
        msg, _override, _level = _block(used, mode=BUDGET_NUDGE_THRESHOLDS, prev=prev)
        if msg is not None:
            spoke_at.append(used)
        prev = used
    assert spoke_at == [20_000, 40_000, 60_000, 80_000]

def test_thresholds_leaping_several_bands_at_once_speaks_once_then_re_arms():
    # 10% -> 70% skips two bands: ONE line, not two. 80% is still ahead, so the
    # next leap over it speaks again.
    leap, _override, _level = _block(70_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=10_000)
    assert leap is not None
    later, _override2, _level2 = _block(85_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=70_000)
    assert later is not None

def test_thresholds_still_delivers_the_hard_write_nudge_on_a_silent_turn():
    msg, override, level = _block(
        30_000, mode=BUDGET_NUDGE_THRESHOLDS, prev=21_000, reads=READS_NUDGE_HARD
    )
    assert override == "required" and level == "hard"
    assert "Budget:" not in msg["content"]

def test_thresholds_never_fires_without_a_budget():
    # No cap -> no fractions -> no reminder, and no ZeroDivisionError.
    msg, _override, _level = build_steering_block(
        used_tokens=5_000, max_budget_tokens=0, step_count=1, max_steps=10,
        reads=0, has_write=True, has_structured_output=False, structured_override=None,
        budget_nudge_mode=BUDGET_NUDGE_THRESHOLDS, prev_used_tokens=0,
    )
    assert msg is None


# --------------------------------------------------------------------------
# through the run loop (the env var is read at the call site)
# --------------------------------------------------------------------------

def _runner(used_tokens):
    # History ends on a tool message, so the block rides in the shaped copy only
    # and llm.calls shows exactly what the model was handed.
    state = SessionState(
        messages=[{"role": "tool", "content": "r"}],
        used_tokens=used_tokens,
        step_count=3,
        turn=TurnEnforcementState(reads_since_last_edit=0),
    )
    llm = FakeLLM([llm_response(content="done") for _ in range(8)])
    return build_runner(
        state=state, llm=llm, agent=_agent_with_tools("file_read", "apply_patch"),
        max_budget_tokens=BUDGET, max_steps=40,
    ), state

def _budget_lines(llm):
    return [
        call for call in llm.calls
        if any("Budget:" in (m.get("content") or "") for m in call["messages"])
    ]

def test_run_loop_default_env_sends_the_budget_line_every_turn(monkeypatch):
    monkeypatch.delenv(BUDGET_NUDGE_ENV_VAR, raising=False)
    runner, state = _runner(10_000)
    run(runner.call_llm(runner.build_tool_schemas()))
    state.used_tokens = 12_000
    run(runner.call_llm(runner.build_tool_schemas()))
    assert len(_budget_lines(runner.llm)) == 2

def test_run_loop_off_env_sends_no_budget_line(monkeypatch):
    monkeypatch.setenv(BUDGET_NUDGE_ENV_VAR, "off")
    runner, _state = _runner(10_000)
    run(runner.call_llm(runner.build_tool_schemas()))
    assert _budget_lines(runner.llm) == []
    # and nothing spurious was appended to the prompt either
    assert all(m["role"] != "user" for m in runner.llm.calls[0]["messages"])

def test_run_loop_thresholds_env_speaks_only_on_the_crossing_turn(monkeypatch):
    monkeypatch.setenv(BUDGET_NUDGE_ENV_VAR, "thresholds")
    runner, state = _runner(10_000)          # 10% — below the first band
    run(runner.call_llm(runner.build_tool_schemas()))
    state.used_tokens = 25_000               # crosses 20%
    run(runner.call_llm(runner.build_tool_schemas()))
    state.used_tokens = 30_000               # same band — silent again
    run(runner.call_llm(runner.build_tool_schemas()))
    seen = [
        any("Budget:" in (m.get("content") or "") for m in call["messages"])
        for call in runner.llm.calls
    ]
    assert seen == [False, True, False]

def test_run_loop_thresholds_does_not_re_announce_bands_already_spent(monkeypatch):
    # A session whose FIRST steering block is built at 50% spent (a resumed or
    # mid-flight agent) must not be told about the 20% and 40% bands it passed
    # before this process was watching: the first turn compares spend against
    # itself and crosses nothing.
    monkeypatch.setenv(BUDGET_NUDGE_ENV_VAR, "thresholds")
    runner, state = _runner(50_000)
    run(runner.call_llm(runner.build_tool_schemas()))
    assert _budget_lines(runner.llm) == []
    state.used_tokens = 61_000               # now crosses 60% for real
    run(runner.call_llm(runner.build_tool_schemas()))
    assert len(_budget_lines(runner.llm)) == 1
