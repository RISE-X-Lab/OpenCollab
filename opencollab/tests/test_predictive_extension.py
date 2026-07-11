"""STEP 4 — predictive overshoot guard + single-justified-extension valve.

* T1 (PREDICTIVE GUARD) — a large per-turn-cost EWMA trips the wind-down ONE turn
  EARLY (``used_tokens + ewma_turn_cost >= explore_threshold`` while
  ``used_tokens`` is still below the threshold), so the protected submit turn
  completes WITHIN the reserve and the budget is never overrun. The EWMA's
  influence is capped (an anomalous expensive turn cannot wind the scout down far
  too early) and the predictive term only applies in the deadline band. off ==
  reference: with enforcement off the predictive term is never computed.

* T2 (EXTENSION VALVE) — at a wind-down trip the scout is offered commit-or-justify
  (submit_findings | request_extension). A concrete, falsifiable, NOVEL reason
  GRANTS exactly ONE bounded extension (one more read, then forced submit); a
  SECOND is denied by the hard cap; a vacuous/duplicate reason is denied
  immediately and the scout is force-committed. off == reference: with enforcement
  off the valve never offers even with the tool wired.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

from opencollab.application.event_bus import EventBus
from opencollab.application.extension_valve import (
    REQUEST_EXTENSION_TOOL_NAME,
    RequestExtensionTool,
    judge_extension_reason,
)
from opencollab.application.session_run import (
    DEFAULT_EWMA_ALPHA,
    DEFAULT_MAX_EXTENSIONS,
    ENFORCEMENT_OFF,
    ENFORCEMENT_ON,
    SessionRunUseCase,
)
from opencollab.application.submit_findings import SUBMIT_TOOL_NAME, SubmitFindingsTool
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Shared fakes (mirror test_session_wind_down.py / test_progress_watchdog.py).
# --------------------------------------------------------------------------- #


def llm_response(content=None, tool_calls=None, total_tokens=5, input_tokens=1, finish_reason="stop"):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(total_tokens=total_tokens, input_tokens=input_tokens),
        finish_reason=finish_reason,
        reasoning=None,
    )


def tool_call(call_id="call-1", name="submit_findings", arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def ext_call(reason, call_id="ext-1"):
    return tool_call(
        call_id=call_id, name=REQUEST_EXTENSION_TOOL_NAME, arguments=json.dumps({"reason": reason})
    )


class FakeLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "tool_choice": kwargs.get("tool_choice"),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)


class FakeToolExecution:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ToolProcessingResult()

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        return self.result


class CapturingToolExecution:
    """Runs tools for real against the agent's live toolset (like the dispatcher):
    submit_findings / request_extension / file_read invoke the real tool; any other
    name returns the same "unknown tool" error the real dispatcher produces."""

    def __init__(self, agent):
        self.agent = agent
        self.calls = []

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        messages = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            tool = self.agent.find_tool(name)
            if tool is None:
                available = [t.name for t in self.agent.tools]
                content = f"Error: unknown tool '{name}'. Available: {available}"
            else:
                params = json.loads(tc["function"].get("arguments") or "{}")
                content = await tool.execute_with_runtime(params, None)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
        return ToolProcessingResult(messages_to_append=messages)


class _ReadStub:
    """Schema-only read stub (never executed) — for tests that don't read."""

    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


class _ExecReadStub:
    """An executable read stub: returns a fixed file-ish payload."""

    name = "file_read"

    def __init__(self, output="File: parser.py (1 lines)\n1\tdef f(): return 1"):
        self._output = output

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}

    async def execute_with_runtime(self, params, runtime):
        return self._output


def build_runner(*, state, agent, llm, event_bus=None, tool_execution=None, **kwargs):
    return SessionRunUseCase(
        agent=agent,
        state=state,
        llm=llm,
        event_publisher=event_bus if event_bus is not None else EventBus(None),
        tool_execution=tool_execution if tool_execution is not None else FakeToolExecution(),
        **kwargs,
    )


def _agent_with_submit():
    return Agent(name="scout", system_prompt="s", tools=[_ReadStub(), SubmitFindingsTool()])


def _captured():
    return {
        "findings": [
            {
                "aspect": "bug origin",
                "claim": "off-by-one in slice bound",
                "evidence_anchor": "parser.py:42",
                "verified": True,
                "confidence": "high",
            }
        ],
        "summary": "root cause located",
        "insufficient_evidence": False,
    }


_FORCED_SUBMIT_CHOICE = {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}}
_GOOD_REASON = "reading parser.py line 88 to confirm the off-by-one hypothesis"


# --------------------------------------------------------------------------- #
# Knob sanity + EWMA maintenance.
# --------------------------------------------------------------------------- #


def test_default_knobs():
    assert DEFAULT_MAX_EXTENSIONS == 1
    assert 0.0 < DEFAULT_EWMA_ALPHA <= 1.0


def test_ewma_seeds_then_smooths_from_turn_deltas():
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}])
    llm = FakeLLM(
        [
            llm_response(tool_calls=[tool_call(name="file_read")], total_tokens=10_000, finish_reason="tool_calls"),
            llm_response(content="done", total_tokens=2_000),
        ]
    )
    runner = build_runner(
        state=state, agent=agent, llm=llm, max_budget_tokens=10_000_000,
        enforcement_strength=ENFORCEMENT_ON,
    )
    run(runner.run_loop())
    # First sample seeds the EWMA; the second is exponentially smoothed.
    expected = DEFAULT_EWMA_ALPHA * 2_000 + (1 - DEFAULT_EWMA_ALPHA) * 10_000
    assert abs(runner._ewma_turn_cost - expected) < 1e-6


# --------------------------------------------------------------------------- #
# T1 — PREDICTIVE OVERSHOOT GUARD.
# --------------------------------------------------------------------------- #


def test_t1_predictive_guard_winds_down_one_turn_early_within_reserve():
    # used=70k < threshold=75k, so the PLAIN budget trigger does NOT fire; only the
    # predictive guard (70k + min(ewma,cap)) can. STEP 2C: the guard now fires only
    # when the predicted landing exceeds ``threshold + margin`` (margin = 40% of the
    # 25k reserve = 10k -> 85k). A LARGE turn (ewma=20k -> predicted 90k >= 85k) still
    # trips EARLY, leaving the reserve for the protected submit turn.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=70_000)
    llm = FakeLLM(
        [
            llm_response(
                tool_calls=[tool_call(arguments=json.dumps(_captured()))],
                total_tokens=4_000,
                finish_reason="tool_calls",
            )
        ]
    )
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    runner._ewma_turn_cost = 20_000  # large per-turn cost (exceeds the 10k margin)

    run(runner.run_loop(capture_done))

    # Tripped EARLY: at 70k, below the 75k threshold (plain budget trigger inert).
    assert state.wind_down_done is True
    assert state.wind_down_token_mark == 70_000
    assert state.wind_down_token_mark < 75_000
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert agent.tool_choice == _FORCED_SUBMIT_CHOICE
    assert len(llm.calls) == 1  # exactly the one protected submit turn
    assert submit.captured is not None
    # Reserve never overrun: the submit turn (4k) finished well inside the 100k cap.
    assert state.used_tokens == 74_000
    assert state.used_tokens < 100_000


def test_2c_relaxation_keeps_a_live_lead_with_comfortable_slack():
    # STEP 2C: used=66k, threshold=75k -> 9k of slack (the 6-10k band the bare guard
    # severed leads in). A normal 10k turn: the BARE guard (margin 0) tripped
    # (66k+10k=76k >= 75k); the relaxed guard (margin = 40% of the 25k reserve = 10k)
    # does NOT (76k < 85k), so the scout keeps exploring instead of being chopped.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=66_000)
    llm = FakeLLM([llm_response(content="still exploring", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    runner._ewma_turn_cost = 10_000
    # Sanity: with NO margin (the old behavior) this same state WOULD trip.
    assert (66_000 + min(10_000, 25_000)) >= (100_000 - 25_000)
    run(runner.run_loop())
    # ...but the relaxed guard leaves the lead alone: no wind-down, normal turn to DONE.
    assert state.wind_down_done is False
    assert state.phase is SessionPhase.DONE
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


def test_2c_genuine_single_turn_overshoot_still_trips_with_slack():
    # The single-turn-overshoot protection (the whole point of STEP 4) survives 2C:
    # comfortable slack (used=66k, 9k below threshold) but ONE turn is large enough to
    # spend more than the margin of the reserve (ewma=60k -> capped at the 25k reserve
    # -> predicted 91k >= 85k) -> STILL trips early so the protected submit fits.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=66_000)
    llm = FakeLLM(
        [
            llm_response(
                tool_calls=[tool_call(arguments=json.dumps(_captured()))],
                total_tokens=3_000,
                finish_reason="tool_calls",
            )
        ]
    )
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    runner._ewma_turn_cost = 60_000  # huge; capped at the 25k reserve -> predicted 91k
    run(runner.run_loop(capture_done))
    assert state.wind_down_done is True
    assert state.wind_down_token_mark == 66_000
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert submit.captured is not None
    assert state.used_tokens == 69_000  # submit turn finished well inside the cap


def test_2c_explicit_margin_override_restores_bare_guard():
    # The default margin is 40% of the reserve, but an explicit ``predictive_margin``
    # overrides it. margin=0 collapses the relaxed guard back to the bare
    # ``predicted >= threshold`` rung, proving the knob actually drives the trigger.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=66_000)
    llm = FakeLLM([llm_response(content="x", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        predictive_margin=0,
    )
    runner._ewma_turn_cost = 10_000
    # margin 0 -> bare guard -> trips (66k + 10k >= 75k); the default margin would not.
    assert runner._predictive_overshoot(75_000) is True
    default_runner = build_runner(
        state=SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=66_000),
        agent=_agent_with_submit(), llm=FakeLLM([]),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    default_runner._ewma_turn_cost = 10_000
    assert default_runner._predictive_overshoot(75_000) is False


def test_predictive_does_not_fire_below_threshold_without_ewma():
    # Same 70k/75k setup but no measured per-turn cost (ewma=0) -> predictive term
    # inert -> no early trip; the scout takes a normal turn. Proves the early trip is
    # the EWMA's doing, not the threshold.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=70_000)
    llm = FakeLLM([llm_response(content="still exploring", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    run(runner.run_loop())
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


def test_predictive_cap_prevents_winding_down_far_too_early():
    # An ANOMALOUS huge EWMA must not wind the scout down across the whole run: the
    # influence is capped at the reserve, so at used=40k (threshold 75k) predicted =
    # 40k + min(huge, 25k) = 65k < 75k -> NO trip. Proves the cap == deadline band.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=40_000)
    llm = FakeLLM([llm_response(content="still exploring", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
    )
    runner._ewma_turn_cost = 100_000  # absurd anomaly
    run(runner.run_loop())
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


def test_predictive_guard_off_is_inert():
    # off == reference: enforcement off -> the predictive term is never computed even
    # with a huge EWMA; the model runs its normal turn to DONE.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=70_000)
    llm = FakeLLM([llm_response(content="answer", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_OFF,
    )
    runner._ewma_turn_cost = 100_000
    result = run(runner.run_loop())
    assert result == "answer"
    assert state.phase is SessionPhase.DONE
    assert state.wind_down_done is False


# --------------------------------------------------------------------------- #
# T2 — SINGLE-JUSTIFIED-EXTENSION VALVE.
# --------------------------------------------------------------------------- #


def test_judge_extension_reason_grants_concrete_denies_vacuous_absent_duplicate():
    granted, why = judge_extension_reason(_GOOD_REASON, [])
    assert granted is True and why == "granted"
    # falsifiability keyword alone (no file anchor) also qualifies.
    assert judge_extension_reason("need to verify whether the cache is ever invalidated", [])[0] is True
    # vacuous filler -> denied.
    assert judge_extension_reason("let me keep looking a bit more", [])[0] is False
    # absent / whitespace -> denied.
    assert judge_extension_reason("", [])[0] is False
    assert judge_extension_reason("   ", [])[0] is False
    # too short -> denied.
    assert judge_extension_reason("read parser.py", [])[1] == "too_vacuous"
    # duplicate of a prior granted reason -> denied as non-novel.
    g, why = judge_extension_reason(_GOOD_REASON, [_GOOD_REASON])
    assert g is False and why == "duplicate"


def _valve_runner(agent, llm, *, max_extensions=1, extension_tool=None):
    return build_runner(
        state=agent_state(), agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=extension_tool, max_extensions=max_extensions,
    )


def agent_state():
    # 80k used, threshold 75k -> the brake trips on the very first precheck.
    return SessionState(messages=[{"role": "user", "content": "go"}], used_tokens=80_000)


def test_t2_justified_extension_granted_then_hard_cap_forces_submit():
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    ext = RequestExtensionTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    llm = FakeLLM(
        [
            # turn 1 (OFFER): the scout justifies with a concrete, falsifiable reason.
            llm_response(
                tool_calls=[ext_call(_GOOD_REASON)],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),
            # turn 2 (granted extension): one more read.
            llm_response(
                tool_calls=[tool_call(name="file_read", arguments="{}")],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),
            # turn 3 (forced wind-down, cap reached): submit.
            llm_response(
                tool_calls=[
                    tool_call(
                        name="submit_findings",
                        arguments=json.dumps(_captured()),
                    )
                ],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),
        ]
    )
    state = agent_state()
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=ext, max_extensions=1,
    )

    run(runner.run_loop(capture_done))

    # Exactly one extension granted, its reason recorded for novelty checks.
    assert state.extensions_granted == 1
    assert state.extension_reasons == [_GOOD_REASON]
    # The SECOND brake (cap reached) force-committed via the non-degradable actuator.
    assert state.wind_down_done is True
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert agent.tool_choice == _FORCED_SUBMIT_CHOICE
    assert submit.captured is not None
    assert len(llm.calls) == 3  # offer + granted read + forced submit


def test_t2_vacuous_reason_denied_immediately_forces_submit():
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    ext = RequestExtensionTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    llm = FakeLLM(
        [
            # OFFER turn: a vacuous reason.
            llm_response(
                tool_calls=[ext_call("let me keep looking a bit more")],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),
            # forced submit turn.
            llm_response(
                tool_calls=[
                    tool_call(
                        name="submit_findings",
                        arguments=json.dumps(_captured()),
                    )
                ],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),
        ]
    )
    state = agent_state()
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=ext, max_extensions=1,
    )

    run(runner.run_loop(capture_done))

    assert state.extensions_granted == 0  # denied
    assert state.wind_down_done is True  # forced straight to submit-only
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert submit.captured is not None
    assert len(llm.calls) == 2  # offer (denied) + forced submit


def test_t2_duplicate_reason_denied_through_runner():
    # With the cap raised to 2, a SECOND offer re-using the FIRST granted reason is
    # denied as non-novel and force-commits — the duplicate path through the runner.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    ext = RequestExtensionTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    llm = FakeLLM(
        [
            llm_response(
                tool_calls=[ext_call(_GOOD_REASON)],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),  # offer 1 -> granted
            llm_response(
                tool_calls=[tool_call(name="file_read", arguments="{}")],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),  # granted read
            llm_response(
                tool_calls=[ext_call(_GOOD_REASON, call_id="ext-2")],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),  # offer 2 -> duplicate
            llm_response(
                tool_calls=[
                    tool_call(
                        name="submit_findings",
                        arguments=json.dumps(_captured()),
                    )
                ],
                total_tokens=1_000,
                finish_reason="tool_calls",
            ),  # forced submit
        ]
    )
    state = agent_state()
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=ext, max_extensions=2,
    )

    run(runner.run_loop(capture_done))

    assert state.extensions_granted == 1  # second (duplicate) denied despite cap=2
    assert state.wind_down_done is True
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert submit.captured is not None
    assert len(llm.calls) == 4


def test_offer_turn_voluntary_submit_commits_without_consuming_extension():
    # Offering does NOT force exploration: a scout may commit on the offer turn. The
    # submit captures -> cancel -> terminal in one turn, no extension consumed.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    ext = RequestExtensionTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    llm = FakeLLM(
        [
            llm_response(
                tool_calls=[tool_call(arguments=json.dumps(_captured()))],
                total_tokens=1_000,
                finish_reason="tool_calls",
            )
        ]
    )
    state = agent_state()
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=ext, max_extensions=1,
    )

    run(runner.run_loop(capture_done))

    assert state.extension_offered is True  # an offer WAS made
    assert state.extensions_granted == 0  # but the scout committed instead
    assert submit.captured is not None
    assert len(llm.calls) == 1


def test_extension_valve_off_is_inert():
    # off == reference: enforcement off -> no offer even with the tool wired and the
    # budget spent; the model runs its normal turn to DONE.
    submit = SubmitFindingsTool()
    ext = RequestExtensionTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    state = agent_state()
    llm = FakeLLM([llm_response(content="answer", total_tokens=1_000)])
    runner = build_runner(
        state=state, agent=agent, llm=llm,
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_OFF,
        extension_tool=ext, max_extensions=1,
    )

    result = run(runner.run_loop())

    assert result == "answer"
    assert state.phase is SessionPhase.DONE
    assert state.extension_offered is False
    assert state.extensions_granted == 0
    assert agent.tool_choice is None


def test_valve_not_offered_when_no_extension_tool_wired():
    # STEP 0/3 parity: with enforcement on but NO request_extension tool, a brake
    # force-commits directly (submit-only), never offering — so STEP 0/3 sessions are
    # byte-for-byte unchanged.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ExecReadStub(), submit])
    state = agent_state()
    llm = FakeLLM(
        [
            llm_response(
                tool_calls=[tool_call(arguments=json.dumps(_captured()))],
                total_tokens=1_000,
                finish_reason="tool_calls",
            )
        ]
    )
    runner = build_runner(
        state=state, agent=agent, llm=llm, tool_execution=CapturingToolExecution(agent),
        max_budget_tokens=100_000, commit_reserve=25_000, enforcement_strength=ENFORCEMENT_ON,
        extension_tool=None,
    )

    run(runner.run_loop(capture_done))

    assert state.extension_offered is False  # never offered
    assert state.wind_down_done is True  # force-committed directly
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert len(llm.calls) == 1
