"""The reads-without-write nudge switch: on (default) / off.

``OPENCOLLAB_WRITE_NUDGE_MODE`` decides whether the harness inserts a
reads-without-write instruction into the turn at all. It exists as a knob
because that instruction is an intervention on the behaviour under measurement:
it orders an agent that has been reading to act itself, at the moment a
delegation decision would be taken.

Two properties are load-bearing here:

* the DEFAULT must stay byte-identical to the pre-knob behaviour, or the runs
  already recorded stop being a control group;
* ``off`` must silence ALL THREE rungs (soft write advice, hard write demand,
  hard structured-output demand) and set no ``tool_choice`` override, while the
  ``[Budget: ...]`` status line — a different quantity — is untouched.
"""

from __future__ import annotations

from session_run_loop_test_support import (
    FakeLLM,
    FakeTracer,
    _agent_with_tool_schemas,
    _agent_with_tools,
    _steering_steps,
    build_runner,
    llm_response,
    run,
)

from opencollab.application.steering import (
    BUDGET_NUDGE_OFF,
    READS_NUDGE_HARD,
    READS_NUDGE_SOFT,
    WRITE_NUDGE_ENV_VAR,
    WRITE_NUDGE_OFF,
    WRITE_NUDGE_ON,
    build_steering_block,
    resolve_write_nudge_mode,
)
from opencollab.domain.session import SessionState, TurnEnforcementState

BUDGET = 100_000


def _block(*, mode=None, reads=0, has_write=True, has_structured=False, write_landed=False,
           budget_mode=None):
    kwargs = dict(
        used_tokens=50_000,
        max_budget_tokens=BUDGET,
        step_count=3,
        max_steps=40,
        reads=reads,
        has_write=has_write,
        has_structured_output=has_structured,
        structured_override={"type": "function", "function": {"name": "structured_output"}},
        write_landed=write_landed,
    )
    if mode is not None:
        kwargs["write_nudge_mode"] = mode
    if budget_mode is not None:
        kwargs["budget_nudge_mode"] = budget_mode
    return build_steering_block(**kwargs)


# --------------------------------------------------------------------------
# mode resolution
# --------------------------------------------------------------------------

def test_unset_or_unknown_env_resolves_to_on():
    # Unset, blank and garbage all fall back to the pre-knob behaviour, so a run
    # that does not opt in is not silently switched to a different instrument.
    assert resolve_write_nudge_mode({}) == WRITE_NUDGE_ON
    assert resolve_write_nudge_mode({WRITE_NUDGE_ENV_VAR: ""}) == WRITE_NUDGE_ON
    assert resolve_write_nudge_mode({WRITE_NUDGE_ENV_VAR: "disabled"}) == WRITE_NUDGE_ON


def test_recognised_values_resolve_case_and_space_insensitively():
    assert resolve_write_nudge_mode({WRITE_NUDGE_ENV_VAR: "on"}) == WRITE_NUDGE_ON
    assert resolve_write_nudge_mode({WRITE_NUDGE_ENV_VAR: " OFF "}) == WRITE_NUDGE_OFF


def test_resolution_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(WRITE_NUDGE_ENV_VAR, "off")
    assert resolve_write_nudge_mode() == WRITE_NUDGE_OFF
    monkeypatch.delenv(WRITE_NUDGE_ENV_VAR)
    assert resolve_write_nudge_mode() == WRITE_NUDGE_ON


# --------------------------------------------------------------------------
# the default is today's behaviour, verbatim
# --------------------------------------------------------------------------

def test_default_still_emits_the_hard_write_demand_and_forces_a_tool():
    # The control group's exact wording and its tool_choice force. If this test
    # goes green with a different default, the 60 recorded runs are no longer
    # comparable to anything run afterwards.
    msg, override, level = _block(reads=READS_NUDGE_HARD)
    assert override == "required" and level == "hard"
    assert (
        f"You have read {READS_NUDGE_HARD} times without making an edit. STOP reading"
        " — your next action MUST be a file_write or apply_patch edit."
    ) in msg["content"]


def test_default_still_emits_the_soft_write_advice():
    msg, override, level = _block(reads=READS_NUDGE_SOFT)
    assert override is None and level == "soft"
    assert "If you can describe the fix, make it now" in msg["content"]


def test_default_still_emits_the_structured_output_demand():
    # The rung that dominates the reading-analyst arm: no write tool in hand, so
    # the harness demands a structured_output commit from reads >= SOFT.
    msg, override, level = _block(
        reads=READS_NUDGE_SOFT, has_write=False, has_structured=True
    )
    assert level == "hard"
    assert override == {"type": "function", "function": {"name": "structured_output"}}
    assert "your next action MUST be structured_output" in msg["content"]


def test_passing_on_explicitly_matches_the_default():
    for reads, has_write, has_structured in (
        (READS_NUDGE_HARD, True, False),
        (READS_NUDGE_SOFT, True, False),
        (READS_NUDGE_SOFT, False, True),
    ):
        default = _block(reads=reads, has_write=has_write, has_structured=has_structured)
        explicit = _block(
            mode=WRITE_NUDGE_ON, reads=reads, has_write=has_write, has_structured=has_structured
        )
        assert default == explicit


# --------------------------------------------------------------------------
# off silences every rung, and only the rungs
# --------------------------------------------------------------------------

def test_off_silences_the_hard_write_demand_and_the_force():
    msg, override, level = _block(mode=WRITE_NUDGE_OFF, reads=READS_NUDGE_HARD)
    assert override is None and level is None
    assert "STOP reading" not in msg["content"]


def test_off_silences_the_soft_write_advice():
    msg, override, level = _block(mode=WRITE_NUDGE_OFF, reads=READS_NUDGE_SOFT)
    assert override is None and level is None
    assert "make it now" not in msg["content"]


def test_off_silences_the_structured_output_demand():
    msg, override, level = _block(
        mode=WRITE_NUDGE_OFF, reads=READS_NUDGE_SOFT, has_write=False, has_structured=True
    )
    assert override is None and level is None
    assert "structured_output" not in msg["content"]


def test_off_keeps_the_budget_status_line_intact():
    # The budget line is a different quantity and is deliberately not switched
    # off with the nudge: an off run still tells the model what it has left.
    off, _override, _level = _block(mode=WRITE_NUDGE_OFF, reads=READS_NUDGE_HARD)
    quiet, _override2, _level2 = _block(mode=WRITE_NUDGE_OFF, reads=0)
    assert off["content"] == quiet["content"] == "[Budget: ~50k/100k tokens left, ~37 steps left.]"


def test_off_with_the_budget_line_also_off_returns_no_message_at_all():
    msg, override, level = _block(
        mode=WRITE_NUDGE_OFF, reads=READS_NUDGE_HARD, budget_mode=BUDGET_NUDGE_OFF
    )
    assert (msg, override, level) == (None, None, None)


# --------------------------------------------------------------------------
# through the run loop (the env var is read at the call site)
# --------------------------------------------------------------------------

def _runner(reads, *, tools=("file_read", "apply_patch"), tracer=None, schemas=False):
    # History ends on a tool message, so the block rides in the shaped copy only
    # and llm.calls shows exactly what the model was handed.
    state = SessionState(
        messages=[{"role": "tool", "content": "r"}],
        used_tokens=10_000,
        step_count=3,
        turn=TurnEnforcementState(reads_since_last_edit=reads),
        aid=7,
    )
    llm = FakeLLM([llm_response(content="done") for _ in range(8)])
    return build_runner(
        state=state, llm=llm, tracer=tracer,
        agent=(_agent_with_tool_schemas if schemas else _agent_with_tools)(*tools),
        max_budget_tokens=BUDGET, max_steps=40,
    ), state


def _prompt_text(llm):
    return "\n".join(
        m.get("content") or "" for call in llm.calls for m in call["messages"]
    )


def test_run_loop_default_env_nudges_traces_and_forces(monkeypatch):
    monkeypatch.delenv(WRITE_NUDGE_ENV_VAR, raising=False)
    tracer = FakeTracer()
    runner, _state = _runner(READS_NUDGE_HARD, tracer=tracer)
    run(runner.call_llm(runner.build_tool_schemas()))
    assert "STOP reading" in _prompt_text(runner.llm)
    assert runner.llm.calls[0].get("tool_choice") == "required"
    assert len(_steering_steps(tracer)) == 1


def test_run_loop_off_env_sends_no_nudge_no_force_and_no_trace(monkeypatch):
    monkeypatch.setenv(WRITE_NUDGE_ENV_VAR, "off")
    tracer = FakeTracer()
    runner, _state = _runner(READS_NUDGE_HARD, tracer=tracer)
    run(runner.call_llm(runner.build_tool_schemas()))
    assert "STOP reading" not in _prompt_text(runner.llm)
    assert runner.llm.calls[0].get("tool_choice") in (None, "auto")
    assert _steering_steps(tracer) == []
    # the budget line still rides along
    assert "Budget:" in _prompt_text(runner.llm)


def test_run_loop_off_env_leaves_the_tool_schemas_unfiltered(monkeypatch):
    # A hard nudge narrows the offered schemas to the write gate; with the nudge
    # off the model keeps its whole toolset, which is the point of the knob.
    monkeypatch.setenv(WRITE_NUDGE_ENV_VAR, "off")
    runner, _state = _runner(READS_NUDGE_HARD, schemas=True)
    run(runner.call_llm(runner.build_tool_schemas()))
    offered = {
        spec["function"]["name"] for spec in (runner.llm.calls[0]["tools"] or [])
    }
    assert "file_read" in offered
