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
import copy
import json
from types import SimpleNamespace

from opencollab.application.event_bus import EventBus
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.session_run import (
    DEFAULT_LOW_YIELD_M,
    DEFAULT_WATCHDOG_K,
    ENFORCEMENT_OFF,
    ENFORCEMENT_ON,
    SessionRunUseCase,
)
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    SubmitFindingsTool,
    commitment_terminus_payload,
)
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Shared fakes (mirror test_session_wind_down.py).
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
    a ``submit_findings`` call invokes the real tool; any other name returns the
    same "unknown tool" error the real dispatcher produces during wind-down."""

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
    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


def collect_events():
    events = []

    async def sink(event):
        events.append((event.type, copy.deepcopy(event.data)))

    return events, EventBus(sink)


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
    state.steps_since_progress = DEFAULT_WATCHDOG_K  # K no-progress steps
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


def test_t1_watchdog_just_below_k_does_not_trip():
    # K-1 no-progress steps must NOT brake (off-by-one guard).
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.steps_since_progress = DEFAULT_WATCHDOG_K - 1
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
    state.low_yield_since_progress = DEFAULT_LOW_YIELD_M  # M low-yield results
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
    state.steps_since_progress = 99
    state.low_yield_since_progress = 99
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


def test_t2_default_enforcement_off_no_brake():
    # No enforcement_strength passed at all -> off -> high counters are inert.
    agent = _agent_with_submit()
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.steps_since_progress = 50
    state.low_yield_since_progress = 50
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, agent=agent, llm=llm, max_budget_tokens=100_000)
    run(runner.run_loop())
    assert state.wind_down_done is False
    assert state.phase is SessionPhase.DONE


# --------------------------------------------------------------------------- #
# Red-team gate: never brake while a tool result is still un-ingested.
# --------------------------------------------------------------------------- #


def test_watchdog_not_entered_while_a_tool_result_is_pending():
    from opencollab.domain.pending import PendingRow, RowKind, RowStatus

    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.steps_since_progress = 99
    state.low_yield_since_progress = 99
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


def test_watchdog_forced_turn_ignored_escalates_and_latches_forced_unsatisfied():
    # Watchdog fires (budget plentiful); the scout IGNORES the forced tool_choice
    # and calls a now-unknown tool on the forced turn. The actuator escalates with
    # exactly ONE retry (submit-only stays enforced) and latches forced_unsatisfied;
    # the scout then commits on the retry -> terminus "forced".
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=1_000)
    state.steps_since_progress = DEFAULT_WATCHDOG_K
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
    assert state.forced_unsatisfied is True
    # Tool-removal stayed enforced across the retry (the terminal, non-degradable rung).
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    assert llm.calls[0]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert llm.calls[1]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert submit.captured is not None


# --------------------------------------------------------------------------- #
# steps_since_progress counter: per-STEP, resets on hit/write.
# --------------------------------------------------------------------------- #


class FakeAgent:
    def __init__(self, tools=None):
        self.tools = tools or []

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class FakeEventPublisher:
    async def emit(self, event):  # pragma: no cover - trivial sink
        pass


class ScriptedTool:
    def __init__(self, name, outputs):
        self.name = name
        self._outputs = list(outputs)

    async def execute_with_runtime(self, args, runtime):
        return self._outputs.pop(0) if self._outputs else ""


def _event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(type="loop_detected", data={}),
        tool_start=lambda tool, args: SimpleNamespace(type="tool_start", data={}),
        tool_end=lambda tool, latency: SimpleNamespace(type="tool_end", data={}),
    )


def _use_case(state, tool):
    return ToolExecutionUseCase(
        agent=FakeAgent(tools=[tool]),
        environment=None,
        state=state,
        event_publisher=FakeEventPublisher(),
        event_factory=_event_factory(),
    )


def _run_one(state, tool, name, args):
    call = {"id": "c1", "function": {"name": name, "arguments": args}}
    run(_use_case(state, tool).process([call])).apply_to(state)


def test_steps_since_progress_increments_per_no_progress_step_and_resets():
    state = SessionState(messages=[])

    # 1) Novel grep hit -> progress -> counter stays 0.
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"end"}')
    assert state.steps_since_progress == 0

    # 2) Content-duplicate -> no progress -> +1.
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"start"}')
    assert state.steps_since_progress == 1

    # 3) "No matches" -> no progress -> +1.
    _run_one(state, ScriptedTool("grep", ["No matches found for pattern: z"]), "grep", '{"pattern":"z"}')
    assert state.steps_since_progress == 2

    # 4) A NOVEL informative read resets to 0.
    _run_one(
        state,
        ScriptedTool("file_read", ["File: b.py (2 lines)\n1\tdef f(): pass"]),
        "file_read",
        '{"path":"b.py"}',
    )
    assert state.steps_since_progress == 0


def test_steps_since_progress_resets_on_write():
    state = SessionState(messages=[])
    _run_one(state, ScriptedTool("grep", ["No matches found for pattern: z"]), "grep", '{"pattern":"z"}')
    assert state.steps_since_progress == 1
    # A landed write is progress -> reset.
    _run_one(state, ScriptedTool("file_write", ["wrote 3 lines to b.py"]), "file_write", '{"path":"b.py"}')
    assert state.steps_since_progress == 0


def test_steps_since_progress_counts_one_per_step_not_per_result():
    # A single batch of TWO low-yield reads costs ONE watchdog step (per-step, not
    # per-result), while low_yield_since_progress counts both results.
    state = SessionState(messages=[])
    tool = ScriptedTool("grep", ["dupe", "dupe"])
    batch = [
        {"id": "c1", "function": {"name": "grep", "arguments": '{"pattern":"a"}'}},
        {"id": "c2", "function": {"name": "grep", "arguments": '{"pattern":"b"}'}},
    ]
    run(_use_case(state, tool).process(batch)).apply_to(state)
    # First result novel (hit), second a content-dup -> net progress in this batch.
    assert state.steps_since_progress == 0
    assert state.distinct_evidence_count == 1
    assert state.low_yield_since_progress == 1


def test_steps_since_progress_resets_on_user_turn():
    state = SessionState(messages=[])
    state.steps_since_progress = 5
    state.reset_for_user_turn()
    assert state.steps_since_progress == 0
