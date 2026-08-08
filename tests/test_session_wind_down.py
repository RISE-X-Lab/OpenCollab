"""STEP 0 — hardened scout wind-down + enforcement_strength flag + commitment metric.

Two load-bearing tests plus their helpers:

* T1 (REFERENCE INVARIANT) — ``enforcement_strength="off"`` (the default) NEVER
  enters the wind-down branch: a budget-exhausted session behaves byte-for-byte
  as it does today (terminal BUDGET_EXCEEDED, no extra LLM turn, no wind-down
  state set, identical messages/events).
* T2 (WIND-DOWN BEHAVIOR) — ``enforcement_strength="needs-enforcement"`` crossing
  ``explore_threshold`` (no pending tool result) winds down: the agent's tools
  are swapped to submit-only, exactly ONE more CALLING_LLM is allowed, then the
  session goes terminal; and the harvest backstop turns a captured payload into a
  findings report while a chop-before-submit yields a "(partial …)" salvage, never
  a bare "(scout died)".
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from session_run_test_support import (
    CapturingToolExecution,
    FakeLLM,
    build_runner,
    collect_events,
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

from opencollab.application.session_run import (
    ENFORCEMENT_OFF,
    ENFORCEMENT_ON,
)
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    SubmitFindingsTool,
    commitment_terminus_payload,
    harvest_findings,
)
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase, SessionState


def test_configure_enforcement_validates_runtime_knobs():
    runner = build_runner(
        state=SessionState(messages=[]),
        agent=_agent_with_submit(),
        llm=FakeLLM(),
        max_budget_tokens=100,
        commit_reserve=20,
    )

    with pytest.raises(ValueError, match="enforcement_strength"):
        runner.configure_enforcement(enforcement_strength="invalid")
    with pytest.raises(ValueError, match="positive integer"):
        runner.configure_enforcement(
            enforcement_strength=ENFORCEMENT_ON, commit_reserve=True
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        runner.configure_enforcement(
            enforcement_strength=ENFORCEMENT_ON, commit_reserve=101
        )


# --------------------------------------------------------------------------- #
# T1 — REFERENCE INVARIANT: off == today, never winds down.
# --------------------------------------------------------------------------- #


def test_t1_off_budget_exhausted_is_byte_for_byte_today():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=10)
    llm = FakeLLM()  # any LLM call would raise
    runner = build_runner(
        state=state,
        agent=_agent_with_submit(),
        llm=llm,
        event_bus=bus,
        max_budget_tokens=10,
        enforcement_strength=ENFORCEMENT_OFF,
        commit_reserve=25_000,
    )

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []  # no extra wind-down turn
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "budget exceeded: 10 tokens used"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Budget exceeded: 10 tokens used. Session stopped.]",
    }
    assert events == [("error", {"reason": "budget exceeded: 10 tokens used", "aid": -1})]
    # Wind-down state never touched, and tools never swapped.
    assert state.wind_down_done is False
    assert [t.name for t in runner.agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


def test_t1_default_enforcement_is_off():
    # No enforcement_strength passed at all -> defaults to off, no wind-down.
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=10)
    llm = FakeLLM()
    runner = build_runner(state=state, agent=_agent_with_submit(), llm=llm, max_budget_tokens=10)

    run(runner.run_loop())

    assert state.phase is SessionPhase.STOPPED
    assert state.wind_down_done is False
    assert llm.calls == []


@pytest.mark.parametrize(
    ("used_tokens", "team_budget_exhausted", "reason"),
    [
        (100, None, "budget exceeded: 100 tokens used"),
        (
            90,
            lambda: True,
            "team budget exceeded: aggregate spend reached the global cap",
        ),
    ],
)
def test_hard_budget_preempts_new_turn_wind_down(
    used_tokens,
    team_budget_exhausted,
    reason,
):
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        used_tokens=used_tokens,
        wind_down_done=True,
        wind_down_attempts=1,
        wind_down_token_mark=80,
        phase=SessionPhase.DONE,
    )
    state.reset_for_user_turn()
    llm = FakeLLM([llm_response(content="must not run")])
    runner = build_runner(
        state=state,
        agent=_agent_with_submit(),
        llm=llm,
        max_budget_tokens=100,
        enforcement_strength=ENFORCEMENT_ON,
        commit_reserve=20,
        team_budget_exhausted=team_budget_exhausted,
    )

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == reason
    assert state.wind_down_done is False


def test_budget_wind_down_is_not_regranted_on_a_new_user_turn():
    state = SessionState(
        messages=[{"role": "user", "content": "first"}],
        used_tokens=80,
    )
    llm = FakeLLM([llm_response(content="first protected answer", total_tokens=5)])
    runner = build_runner(
        state=state,
        agent=_agent_with_submit(),
        llm=llm,
        max_budget_tokens=100,
        enforcement_strength=ENFORCEMENT_ON,
        commit_reserve=20,
    )

    run(runner.run_loop())
    assert len(llm.calls) == 1
    assert state.used_tokens == 85

    state.reset_for_user_turn()
    runner.reset_runtime_for_user_turn()
    state.append_message({"role": "user", "content": "again"})

    run(runner.run_loop())

    assert len(llm.calls) == 1
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "budget reserve exhausted: protected commit turn already used"


# --------------------------------------------------------------------------- #
# T2 — WIND-DOWN BEHAVIOR.
# --------------------------------------------------------------------------- #


_FORCED_SUBMIT_CHOICE = {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}}


def test_t2_winddown_forces_tool_choice_and_commits_in_one_turn():
    # Provider HONORS the forced tool_choice: the single protected turn calls
    # submit_findings, which captures -> cancel-on-capture -> terminal in one turn.
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    # 80k used, 100k cap, 25k reserve -> explore_threshold = 75k -> crossed.
    state = SessionState(messages=[{"role": "user", "content": "investigate"}], used_tokens=80_000)
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

    # Wind-down fired: flag + token mark; toolset narrowed to submit-only.
    assert state.wind_down_done is True
    assert state.wind_down_token_mark == 80_000
    assert [t.name for t in agent.tools] == [SUBMIT_TOOL_NAME]
    # FIX #1: tool_choice was FORCED to the submit_findings function (both on the
    # agent and on the wire), not "auto"/"required".
    assert agent.tool_choice == _FORCED_SUBMIT_CHOICE
    assert llm.calls[0]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    # Committed on the first forced turn -> exactly one model call, no retry.
    assert len(llm.calls) == 1
    assert submit.captured is not None
    assert state.phase.is_terminal()


def test_t2_winddown_retries_once_on_wrong_tool_then_commits_forced():
    # Provider IGNORES the forced tool_choice (DashScope 400->auto): turn 1 calls a
    # now-unknown tool. FIX #2: one retry is issued (not terminated), and on the
    # retry the model calls submit_findings -> captured -> terminus "forced".
    capture_done = asyncio.Event()
    submit = SubmitFindingsTool(on_capture=capture_done.set)
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=80_000)
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

    # Forced turn + exactly ONE retry.
    assert len(llm.calls) == 2
    # The retry re-stated submit-only availability.
    assert any(
        m["role"] == "system" and "Only submit_findings is available" in (m.get("content") or "")
        for m in state.messages
    )
    # Both turns kept tool_choice forced to submit_findings.
    assert llm.calls[0]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    assert llm.calls[1]["tool_choice"] == _FORCED_SUBMIT_CHOICE
    # FIX #3: terminus reaches "forced" because submit_findings actually fired.
    assert submit.captured is not None
    payload = commitment_terminus_payload(
        role="scout:0", captured=submit.captured, wind_down_done=state.wind_down_done,
        used_tokens=state.used_tokens, max_budget_tokens=100_000,
        wind_down_token_mark=state.wind_down_token_mark, artifact="x",
    )
    assert payload["terminus"] == "forced"


def test_t2_winddown_retry_capped_at_one_then_terminal_strayed():
    # Both the forced turn AND the single retry call a wrong tool -> the loop goes
    # terminal (capped at one retry, no loop). FakeLLM has exactly 2 responses, so a
    # 3rd model call would raise — the cap is enforced by construction.
    submit = SubmitFindingsTool()
    agent = Agent(name="scout", system_prompt="s", tools=[_ReadStub(), submit])
    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=80_000)
    llm = FakeLLM(
        [
            llm_response(tool_calls=[tool_call(name="grep", arguments="{}")], finish_reason="tool_calls"),
            llm_response(tool_calls=[tool_call(name="bash", arguments="{}")], finish_reason="tool_calls"),
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

    run(runner.run_loop())

    assert len(llm.calls) == 2  # capped: forced + 1 retry, NO third turn
    assert state.phase is SessionPhase.STOPPED
    assert submit.captured is None
    payload = commitment_terminus_payload(
        role="scout:0", captured=None, wind_down_done=True,
        used_tokens=state.used_tokens, max_budget_tokens=100_000,
        wind_down_token_mark=state.wind_down_token_mark, artifact="",
    )
    assert payload["terminus"] == "strayed"


def test_restored_wind_down_does_not_regrant_an_already_allocated_retry(tmp_path):
    session = Session(agent=_agent_with_submit(), llm=FakeLLM())
    session.state.wind_down_done = True
    # This is the durable checkpoint immediately after the sole retry was
    # allocated and before its provider call could complete.
    session.state.wind_down_attempts = 2
    path = tmp_path / "wind-down-retry.json"
    session.save(str(path))

    restored_llm = FakeLLM()
    restored = load_session(path, agent=_agent_with_submit(), llm=restored_llm)
    restored.runner.configure_enforcement(
        enforcement_strength=ENFORCEMENT_ON,
        commit_reserve=25_000,
    )

    run(restored.run_loop())

    assert restored.state.wind_down_attempts == 2
    assert restored_llm.calls == []
    assert restored.state.phase is SessionPhase.STOPPED


def test_legacy_wind_down_snapshot_without_attempts_stops_conservatively(tmp_path):
    session = Session(agent=_agent_with_submit(), llm=FakeLLM())
    session.state.wind_down_done = True
    path = tmp_path / "legacy-wind-down.json"
    session.save(str(path))
    snapshot = json.loads(path.read_text())
    del snapshot["session_state"]["wind_down_attempts"]
    del snapshot["session_state"]["budget_reserve_consumed"]
    path.write_text(json.dumps(snapshot))

    restored_llm = FakeLLM()
    restored = load_session(path, agent=_agent_with_submit(), llm=restored_llm)
    restored.runner.configure_enforcement(
        enforcement_strength=ENFORCEMENT_ON,
        commit_reserve=25_000,
    )

    run(restored.run_loop())

    assert restored.state.wind_down_attempts == 2
    assert restored.state.budget_reserve_consumed is True
    assert restored_llm.calls == []


def test_t2_winddown_not_entered_while_a_tool_result_is_pending():
    # Red-team gate: never force-commit while a tool call's result has not been
    # ingested. With a pending row in the table the threshold-crossed precheck must
    # NOT wind down; it falls through to the normal (budget) path instead.
    from opencollab.domain.pending import PendingRow, RowKind, RowStatus

    state = SessionState(messages=[{"role": "user", "content": "x"}], used_tokens=80_000)
    state.budget_reserve_consumed = True
    state.pending_events.add(
        PendingRow(tool_call_id="t1", kind=RowKind.CHILD_AGENT, order=0, status=RowStatus.PENDING)
    )
    agent = _agent_with_submit()
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        max_budget_tokens=100_000,
        commit_reserve=25_000,
        enforcement_strength=ENFORCEMENT_ON,
    )

    run(runner.run_loop())

    # No wind-down: tools untouched, no system wind-down message injected.
    assert state.wind_down_done is False
    assert [t.name for t in agent.tools] == ["file_read", SUBMIT_TOOL_NAME]


# --------------------------------------------------------------------------- #
# T2 (harvest backstop) — captured payload -> report; chop -> "(partial …)".
# --------------------------------------------------------------------------- #


def _captured(insufficient=False, findings=None):
    return {
        "findings": findings
        if findings is not None
        else [
            {
                "aspect": "bug origin",
                "claim": "off-by-one in slice bound",
                "evidence_anchor": "fs.py:42",
                "verified": True,
                "confidence": "high",
            }
        ],
        "summary": "root cause located",
        "insufficient_evidence": insufficient,
    }


def test_harvest_uses_captured_payload_as_report():
    report = harvest_findings(_captured(), fallback_text="", messages=[])
    assert "root cause located" in report
    assert "fs.py:42" in report
    assert "off-by-one" in report
    assert "scout died" not in report


def test_harvest_chop_before_submit_yields_partial_not_scout_died():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [tool_call(name="grep")]},
        {"role": "tool", "tool_call_id": "call-1", "content": "fs.py:42:    end = start + n"},
        {"role": "assistant", "tool_calls": [tool_call(name="file_read")]},
        {"role": "tool", "tool_call_id": "call-2", "content": "def slice(...): ..."},
    ]
    report = harvest_findings(None, fallback_text="", messages=messages)
    assert "partial" in report
    assert "2 tool results" in report
    assert "fs.py:42" in report
    assert "scout died" not in report


def test_harvest_bounds_large_raw_transcript_and_reports_omissions():
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "content": f"{index}:" + "x" * 500}
        for index in range(10_000)
    ]
    report = harvest_findings(None, fallback_text="", messages=messages)
    assert len(report) <= 16_000
    assert "omitted" in report
    assert "9999:" in report


def test_harvest_falls_back_to_text_when_no_capture():
    report = harvest_findings(None, fallback_text="here is my prose report", messages=[])
    assert report == "here is my prose report"


def test_harvest_truly_empty_returns_empty_string():
    # Nothing captured, no text, no tool results -> "" so the caller's own
    # fallback decides; harvest itself never fabricates.
    assert harvest_findings(None, fallback_text="", messages=[]) == ""


# --------------------------------------------------------------------------- #
# submit_findings tool — cite-or-abstain post-validation.
# --------------------------------------------------------------------------- #


class _Runtime:
    pass


def _exec(tool, params):
    return run(tool.execute_with_runtime(params, _Runtime()))


def test_submit_findings_captures_valid_cited_payload():
    captured_flag = {"hit": False}
    tool = SubmitFindingsTool(on_capture=lambda: captured_flag.__setitem__("hit", True))
    out = _exec(tool, _captured())
    assert "accepted" in out.lower()
    assert tool.captured is not None
    assert captured_flag["hit"] is True


def test_submit_findings_rejects_verified_without_anchor():
    tool = SubmitFindingsTool()
    bad = _captured(
        findings=[{"aspect": "a", "claim": "c", "evidence_anchor": "", "verified": True, "confidence": "low"}]
    )
    out = _exec(tool, bad)
    assert tool.captured is None  # not captured
    assert "cite" in out.lower() or "evidence_anchor" in out.lower()


def test_submit_findings_accepts_insufficient_evidence_abstention():
    tool = SubmitFindingsTool()
    out = _exec(tool, _captured(insufficient=True, findings=[]))
    assert tool.captured is not None  # abstaining is a valid, non-penalized outcome
    assert "accepted" in out.lower()


def test_submit_findings_rejects_empty_non_abstaining_report():
    captured_flag = {"hit": False}
    tool = SubmitFindingsTool(on_capture=lambda: captured_flag.__setitem__("hit", True))
    out = _exec(
        tool,
        {"findings": [], "summary": "", "insufficient_evidence": False},
    )
    assert tool.captured is None
    assert tool.terminal_capture_accepted is False
    assert captured_flag["hit"] is False
    assert "at least one finding" in out.lower()


def test_submit_findings_requires_explanation_when_abstaining():
    tool = SubmitFindingsTool()
    out = _exec(
        tool,
        {"findings": [], "summary": "   ", "insufficient_evidence": True},
    )
    assert tool.captured is None
    assert "explain" in out.lower()


def test_submit_findings_abstention_cannot_bypass_verified_anchor():
    tool = SubmitFindingsTool()
    bad = _captured(
        insufficient=True,
        findings=[
            {
                "aspect": "a",
                "claim": "unsupported",
                "evidence_anchor": "",
                "verified": True,
                "confidence": "low",
            }
        ],
    )

    out = _exec(tool, bad)

    assert tool.captured is None
    assert "evidence_anchor" in out


# --------------------------------------------------------------------------- #
# commitment-terminus metric payload shape.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Workflow-level wiring: WorkflowContext.agent enforcement path (harvest + metric)
# and the off-parity guard.
# --------------------------------------------------------------------------- #


class _FakeTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, step_type, payload, tokens=0, latency=0.0):
        self.steps.append({"step_type": step_type, "payload": copy.deepcopy(payload)})


class _FakeRunner:
    def __init__(self):
        self.configured = None

    def configure_enforcement(self, *, enforcement_strength, commit_reserve):
        self.configured = (enforcement_strength, commit_reserve)


class _FakeState:
    def __init__(self, used_tokens=0):
        self.used_tokens = used_tokens
        self.wind_down_done = False
        self.wind_down_token_mark = 0
        self.messages = []


class _EnforcedFakeSession:
    """A scripted session that simulates a scout calling submit_findings: when
    ``capture`` is set, run_loop stashes it on the injected submit tool."""

    def __init__(self, *, capture=None, reply="", used_tokens=30_000, max_budget_tokens=100_000):
        self.runner = _FakeRunner()
        self.state = _FakeState(used_tokens)
        self.max_budget_tokens = max_budget_tokens
        self._capture = capture
        self.reply = reply
        self.submit_tool = None

    async def add_user_message(self, content):
        self.state.messages.append({"role": "user", "content": content})

    async def run_loop(self, cancel_event=None):
        if self._capture is not None and self.submit_tool is not None:
            self.submit_tool.captured = self._capture
        return self.reply


class _EnforcedFakeFactory:
    def __init__(self, session):
        self.session = session
        self.builds = []

    def build_workflow_session(self, *, prompt, budget, tools=None, isolation=False,
                               label=None, tool_choice=None, thinking=None):
        self.builds.append({"tools": list(tools or []), "label": label})
        for tool in tools or []:
            if getattr(tool, "name", None) == SUBMIT_TOOL_NAME:
                self.session.submit_tool = tool
        return self.session


def _ctx_with(factory, tracer):
    from opencollab.application.workflow import WorkflowContext

    return WorkflowContext(factory, tracer=tracer, budget_total=None)


def test_enforced_agent_injects_submit_configures_runner_harvests_and_emits_metric():
    tracer = _FakeTracer()
    session = _EnforcedFakeSession(capture=_captured(), reply="", used_tokens=30_000)
    factory = _EnforcedFakeFactory(session)
    ctx = _ctx_with(factory, tracer)

    result = run(
        ctx.agent(
            "scout the bug",
            tools=[_ReadStub()],
            label="scout:0:bug-origin",
            enforcement_strength=ENFORCEMENT_ON,
            commit_reserve=25_000,
        )
    )

    # Harvested from the captured submit_findings payload (not the empty reply).
    assert "root cause located" in result
    assert "fs.py:42" in result
    # submit_findings was injected alongside the read tools.
    assert any(getattr(t, "name", None) == SUBMIT_TOOL_NAME for t in factory.builds[0]["tools"])
    # The runner's brake was armed with the requested strength + reserve.
    assert session.runner.configured == (ENFORCEMENT_ON, 25_000)
    # Exactly one commitment_terminus metric, classified voluntary (committed before
    # any wind-down), with the right anchor count.
    metrics = [s for s in tracer.steps if s["step_type"] == "commitment_terminus"]
    assert len(metrics) == 1
    payload = metrics[0]["payload"]
    assert payload["terminus"] == "voluntary"
    assert payload["role"] == "scout:0:bug-origin"
    assert payload["evidence_anchor_count"] == 1
    assert payload["artifact_nonempty"] is True


def test_commitment_trace_failure_does_not_overturn_harvested_result():
    class ThrowingTracer:
        def log_step(self, **_kwargs):
            raise OSError("trace sink unavailable")

    session = _EnforcedFakeSession(capture=_captured(), reply="")
    ctx = _ctx_with(_EnforcedFakeFactory(session), ThrowingTracer())

    result = run(
        ctx.agent(
            "scout the bug",
            tools=[_ReadStub()],
            label="scout:0",
            enforcement_strength=ENFORCEMENT_ON,
        )
    )

    assert "root cause located" in result
    assert ctx.trace_failures == (
        {"step_type": "commitment_terminus", "exception_type": "OSError"},
    )


def test_off_default_does_not_inject_submit_or_emit_metric():
    tracer = _FakeTracer()
    session = _EnforcedFakeSession(reply="plain text report", used_tokens=10_000)
    factory = _EnforcedFakeFactory(session)
    ctx = _ctx_with(factory, tracer)

    # No enforcement_strength passed -> off -> unchanged _run_agent path.
    result = run(ctx.agent("scout the bug", tools=[_ReadStub()], label="scout:0"))

    assert result == "plain text report"
    assert all(getattr(t, "name", None) != SUBMIT_TOOL_NAME for t in factory.builds[0]["tools"])
    assert session.runner.configured is None  # runner never armed
    assert [s for s in tracer.steps if s["step_type"] == "commitment_terminus"] == []
