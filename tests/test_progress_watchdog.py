"""STEP 3 — progress watchdog + anti-windup tool-removal actuator + low-yield brake.

Two additional triggers into the SAME forced-commit actuator (``_enter_wind_down``)
the budget-threshold wind-down uses, so a scout that spins WHILE BUDGET IS STILL
PLENTIFUL is force-committed instead of grepping to the cap. Both key strictly on
the STEP-1 information-gain sensor via the DISTINCT brake gate (``_brake_on``),
never on ``has_write``.

* T1 (WATCHDOG) — ``steps_since_progress >= K`` with budget remaining routes the
  scout through ``_enter_wind_down``: tools are physically narrowed to submit-only
  (the non-degradable actuator), ``tool_choice`` is forced to the submit function,
  and a committed turn reaches terminus "forced".
* T2 (LOW-YIELD BRAKE + off==reference) — ``low_yield_since_progress >= M`` trips
  the same actuator; AND with enforcement OFF neither brake fires even with the
  counters pinned high (no watchdog/brake, no extra control flow) — byte-for-byte
  the reference behavior.

Plus: the ``steps_since_progress`` counter increments per no-progress STEP and
resets on a hit/write; the red-team pending-result gate blocks a brake while a
tool result is still un-ingested.
"""

from __future__ import annotations

import asyncio
import json

from session_run_test_support import (
    CapturingToolExecution,
    FakeLLM,
    build_runner,
    llm_response,
    run,
    tool_call,
)
from session_run_test_support import (
    ReadStub as _ReadStub,
)
from session_run_test_support import (
    agent_with_submit as _agent_with_submit,
)
from tool_execution_test_support import build_sensor_use_case as _use_case

from opencollab.application.event_bus import EventBus
from opencollab.application.session_run import (
    DEFAULT_LOW_YIELD_M,
    DEFAULT_WATCHDOG_K,
    ENFORCEMENT_OFF,
    ENFORCEMENT_ON,
)
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    SubmitFindingsTool,
    commitment_terminus_payload,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase, SessionState


def _captured():
    return {
        "findings": [
            {
                "aspect": "bug origin",
                "claim": "off-by-one in slice bound",
                "evidence_anchor": "fs.py:42",
                "verified": True,
                "confidence": "high",
            }
        ],
        "summary": "root cause located",
        "insufficient_evidence": False,
    }


_FORCED_SUBMIT_CHOICE = {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}}


# --------------------------------------------------------------------------- #
# Knob sanity.
# --------------------------------------------------------------------------- #


def test_default_knobs():
    assert DEFAULT_WATCHDOG_K == 4
    assert DEFAULT_LOW_YIELD_M == 3


# --------------------------------------------------------------------------- #
# T1 — WATCHDOG: spin with budget remaining -> forced commit via _enter_wind_down.
# --------------------------------------------------------------------------- #


def test_t1_watchdog_trips_with_budget_remaining_and_forces_commit():
    # Budget is PLENTIFUL (1k of 100k used, threshold 75k far away) so ONLY the
    # watchdog can trip — the real pathology of spinning while budget is plentiful.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "investigate"}], used_tokens=1_000)
    state.turn.steps_since_progress = DEFAULT_WATCHDOG_K  # K no-progress steps
    llm = FakeLLM(
        [llm_response(tool_calls=[tool_call(arguments=json.dumps(_captured()))], finish_reason="tool_calls")]
    )
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_ON,
    )

    run(runner.run_loop(capture_done))

    # The watchdog routed THROUGH the forced-commit actuator even with budget left.
    assert state.wind_down_done is True
    assert state.used_tokens < 75_000  # budget genuinely was remaining when it tripped
    # Physical tool-removal actuator: toolset narrowed to submit-only + forced choice.
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert agent.tool_choice == _FORCED_SUBMIT_CHOICE
    assert llm.calls[0]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert len(llm.calls) == 1
    assert submit.captured is not None
    assert state.phase.is_terminal()
    # Terminus is reachable as "forced".
    payload = commitment_terminus_payload(
        role="scout:0", captured=submit.captured, wind_down_done=state.wind_down_done,
        used_tokens=state.used_tokens, max_budget_tokens=100_000,
        wind_down_token_mark=state.wind_down_token_mark, artifact="x",
    )
    assert payload["terminus"] == "forced"


def test_watchdog_trips_after_three_malformed_tool_steps():
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(
        messages=[{"role": "user", "content": "investigate"}],
        used_tokens=1_000,
    )
    malformed = [
        llm_response(
            tool_calls=[
                tool_call(
                    call_id=f"broken-{index}",
                    name="file_read",
                    arguments='{"path":',
                )
            ],
            finish_reason="tool_calls",
        )
        for index in range(DEFAULT_WATCHDOG_K)
    ]
    final = llm_response(
        tool_calls=[
            tool_call(
                call_id="submit",
                arguments=json.dumps(_captured()),
            )
        ],
        finish_reason="tool_calls",
    )
    llm = FakeLLM([*malformed, final])
    tool_execution = ToolExecutionUseCase(
        agent=agent,
        environment=None,
        state=state,
        event_publisher=EventBus(None),
    )
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        tool_execution=tool_execution,
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_ON,
    )

    run(runner.run_loop(capture_done))

    assert len(llm.calls) == DEFAULT_WATCHDOG_K + 1
    assert llm.calls[-1]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert state.wind_down_done is True
    assert submit.captured is not None


def test_t1_watchdog_just_below_k_does_not_trip():
    # K-1 no-progress steps must NOT brake (off-by-one guard).
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.turn.steps_since_progress = DEFAULT_WATCHDOG_K - 1
    llm = FakeLLM([llm_response(content="still exploring")])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    run(runner.run_loop())
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


# --------------------------------------------------------------------------- #
# T2 — LOW-YIELD BRAKE + off==reference parity.
# --------------------------------------------------------------------------- #


def test_t2_low_yield_brake_trips_at_m_and_forces_commit():
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "investigate"}], used_tokens=2_000)
    state.turn.low_yield_since_progress = DEFAULT_LOW_YIELD_M  # M low-yield results
    llm = FakeLLM(
        [llm_response(tool_calls=[tool_call(arguments=json.dumps(_captured()))], finish_reason="tool_calls")]
    )
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_ON,
    )

    run(runner.run_loop(capture_done))

    assert state.wind_down_done is True
    assert state.used_tokens < 75_000  # budget remaining
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert agent.tool_choice == _FORCED_SUBMIT_CHOICE
    assert submit.captured is not None
    assert state.phase.is_terminal()


def test_t2_off_pinned_counters_never_brake_reference_behavior():
    # off==reference: enforcement OFF + BOTH sensor counters pinned absurdly high
    # must behave exactly as today — no wind-down, tools untouched, the model runs
    # its normal turn and finishes DONE on its plain-text answer.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "investigate"}], used_tokens=2_000)
    state.turn.steps_since_progress = 99
    state.turn.low_yield_since_progress = 99
    llm = FakeLLM([llm_response(content="here is my answer")])
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_OFF,
    )

    result = run(runner.run_loop())

    assert result == "here is my answer"
    assert state.phase is SessionPhase.DONE
    # No brake: wind-down never touched, toolset unchanged, no forced tool_choice.
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]
    assert agent.tool_choice is None
    assert llm.calls[0]["tool_choice"] is None
    assert len(llm.calls) == 1


# --------------------------------------------------------------------------- #
# Red-team gate: never brake while a tool result is still un-ingested.
# --------------------------------------------------------------------------- #


def test_watchdog_not_entered_while_a_tool_result_is_pending():
    from opencollab.domain.pending import PendingRow, RowKind, RowStatus

    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.turn.steps_since_progress = 99
    state.turn.low_yield_since_progress = 99
    state.pending_events.add(
        PendingRow(tool_call_id="t1", kind=RowKind.CHILD_AGENT, order=0, status=RowStatus.PENDING)
    )
    agent = _agent_with_submit()
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    run(runner.run_loop())
    # Pending row blocks the brake -> no wind-down, tools untouched.
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


# --------------------------------------------------------------------------- #
# Anti-windup: a forced turn the scout ignores escalates and latches.
# --------------------------------------------------------------------------- #


def test_watchdog_forced_turn_ignored_escalates_once_then_commits():
    # Watchdog fires (budget plentiful); the scout IGNORES the forced tool_choice
    # and calls a now-unknown tool on the forced turn. The actuator escalates with
    # exactly ONE retry (submit-only stays enforced); the scout then commits on the
    # retry -> terminus "forced".
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.turn.steps_since_progress = DEFAULT_WATCHDOG_K
    llm = FakeLLM(
        [
            llm_response(tool_calls=[tool_call(name="grep", arguments="{}")], finish_reason="tool_calls"),
            llm_response(
                tool_calls=[tool_call(name="submit_findings", arguments=json.dumps(_captured()))],
                finish_reason="tool_calls",
            ),
        ]
    )
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_ON,
    )

    run(runner.run_loop(capture_done))

    assert len(llm.calls) == 2  # forced turn + exactly one retry
    # Tool-removal stayed enforced across the retry (the terminal, non-degradable rung).
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert llm.calls[0]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert llm.calls[1]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert submit.captured is not None


# --------------------------------------------------------------------------- #
# steps_since_progress counter: per-STEP, resets on hit/write.
# --------------------------------------------------------------------------- #


class ScriptedTool:
    def __init__(self, name, outputs):
        self.name = name
        self._outputs = list(outputs)

    async def execute_with_runtime(self, args, runtime):
        return self._outputs.pop(0) if self._outputs else ""


def _run_one(state, tool, name, args):
    call = {"id": "c1", "function": {"name": name, "arguments": args}}
    run(_use_case(state, tool).process([call])).apply_to(state)


def test_steps_since_progress_resets_on_user_turn():
    state = SessionState(messages=[])
    state.turn.steps_since_progress = 5
    state.reset_for_user_turn()
    assert state.turn.steps_since_progress == 0
