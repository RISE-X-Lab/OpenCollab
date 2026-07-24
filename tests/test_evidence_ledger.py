"""STEP 2 — harness-authored evidence ledger + transcript-only dead-scout synthesizer.

Two load-bearing tests plus their helpers:

* T1 (LEDGER) — a deterministic per-scout evidence ledger accumulates one compact
  card ``{tool, target, outcome, snippet}`` per EXECUTED tool call, built purely
  from the tool-result envelope (no model involvement). The ``outcome`` reuses the
  STEP-1 classification: ``hit`` (novel informative), ``NO-MATCH`` (empty/no-match
  intrinsic low-yield), ``duplicate`` (seen-before, non-novel). The ledger is the
  always-on durable capture FLOOR — it is populated regardless of enforcement_strength
  (like the STEP-1 counters) and never alters control flow (off == reference parity).
* T2 (SYNTHESIZER) — a dead/empty scout (no captured submit_findings payload) with a
  non-empty ledger triggers ONE bounded LLM call whose only tool is submit_findings
  (tool_choice forced, cite-or-abstain) and whose only input is the captured ledger +
  transcript — NO new exploration. It yields a non-empty cited findings report OR a
  valid insufficient_evidence abstention, replacing the vacuous "(scout died)" fallback.
"""

from __future__ import annotations

import asyncio
import copy

from tool_execution_test_support import build_sensor_use_case as _use_case

from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    harvest_findings,
)
from opencollab.domain.session import SessionState, TurnEnforcementState


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# T1 helpers — reuse the STEP-1 tool-execution harness.
# --------------------------------------------------------------------------- #


class ScriptedTool:
    def __init__(self, name, outputs):
        self.name = name
        self._outputs = list(outputs)
        self.calls = []

    async def execute_with_runtime(self, args, runtime):
        self.calls.append(args)
        return self._outputs.pop(0) if self._outputs else ""


def _call(name, args, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args}}


def _run_one(state, tool, name, args):
    result = run(_use_case(state, tool).process([_call(name, args)]))
    result.apply_to(state)
    return result


# --------------------------------------------------------------------------- #
# T1 — LEDGER accumulates correct cards across tool calls.
# --------------------------------------------------------------------------- #


def test_t1_ledger_accumulates_correct_cards_across_tool_calls():
    state = SessionState(messages=[])

    # 1) A novel grep hit -> outcome "hit", target = the pattern, snippet of result.
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"end"}')
    # 2) Content duplicate (different args, identical content) -> "duplicate".
    _run_one(state, ScriptedTool("grep", ["fs.py:42: end = start + n"]), "grep", '{"pattern":"start"}')
    # 3) An empty read -> intrinsic low-yield -> "NO-MATCH".
    _run_one(state, ScriptedTool("file_read", [""]), "file_read", '{"path":"empty.py"}')
    # 4) A "No matches" grep -> intrinsic low-yield -> "NO-MATCH".
    _run_one(state, ScriptedTool("grep", ["No matches found for pattern: zzz"]), "grep", '{"pattern":"zzz"}')
    # 5) A novel file_read -> "hit".
    _run_one(
        state,
        ScriptedTool("file_read", ["File: b.py (2 lines total)\n1\tdef f(): pass"]),
        "file_read",
        '{"path":"b.py"}',
    )

    ledger = state.turn.scout_ledger
    assert len(ledger) == 5
    assert [c["outcome"] for c in ledger] == ["hit", "duplicate", "NO-MATCH", "NO-MATCH", "hit"]
    assert [c["tool"] for c in ledger] == ["grep", "grep", "file_read", "grep", "file_read"]
    # Targets are read straight off the args envelope (path for reads, pattern for grep).
    assert ledger[0]["target"] == "end"
    assert ledger[2]["target"] == "empty.py"
    assert ledger[4]["target"] == "b.py"
    # The salient snippet of the first hit carries the real matched line.
    assert "end = start + n" in ledger[0]["snippet"]


def test_t1_ledger_resets_on_a_fresh_user_turn():
    state = SessionState(messages=[])
    _run_one(state, ScriptedTool("grep", ["a hit"]), "grep", '{"pattern":"a"}')
    assert len(state.turn.scout_ledger) == 1
    state.reset_for_user_turn()
    assert state.turn.scout_ledger == []


def test_t1_harvest_backstop_reads_the_ledger_for_a_partial_salvage():
    # With no captured payload and no prose, harvest prefers the richer classified
    # ledger over a raw message concat, and never emits a bare "(scout died)".
    ledger = [
        {"tool": "grep", "target": "end", "outcome": "hit", "snippet": "fs.py:42: end = start + n"},
        {"tool": "file_read", "target": "fs.py", "outcome": "duplicate", "snippet": "def slice(...)"},
    ]
    report = harvest_findings(None, fallback_text="", messages=[], ledger=ledger)
    assert "partial" in report
    assert "fs.py:42" in report
    assert "[hit]" in report
    assert "scout died" not in report


# --------------------------------------------------------------------------- #
# T2 helpers — workflow-level dead-scout synthesizer.
# --------------------------------------------------------------------------- #

from opencollab.application.session_run import ENFORCEMENT_OFF, ENFORCEMENT_ON  # noqa: E402


def _cited_payload():
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
        "summary": "root cause located from the gathered evidence",
        "insufficient_evidence": False,
    }


def _insufficient_payload():
    return {"findings": [], "summary": "", "insufficient_evidence": True}


class _ReadStub:
    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


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
    def __init__(self, used_tokens=0, scout_ledger=None, messages=None):
        self.used_tokens = used_tokens
        self.wind_down_done = False
        self.wind_down_token_mark = 0
        self.messages = messages if messages is not None else []
        self.turn = TurnEnforcementState(
            scout_ledger=scout_ledger if scout_ledger is not None else []
        )


class _FakeSession:
    """A scripted one-shot session. ``capture`` (when set) is stashed onto the
    injected submit tool by ``run_loop`` to simulate a model calling it."""

    def __init__(self, *, capture=None, reply="", used_tokens=30_000,
                 max_budget_tokens=100_000, scout_ledger=None, messages=None):
        self.runner = _FakeRunner()
        self.state = _FakeState(used_tokens, scout_ledger=scout_ledger, messages=messages)
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


class _ScriptedFactory:
    """Returns scripted sessions in build order; routes any injected submit tool
    onto the session so its run_loop can capture into it."""

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.builds = []

    def build_workflow_session(self, *, prompt, budget, tools=None, isolation=False,
                               label=None, tool_choice=None, thinking=None):
        self.builds.append(
            {"tools": list(tools or []), "label": label, "tool_choice": tool_choice, "prompt": prompt}
        )
        session = self.sessions.pop(0)
        for tool in tools or []:
            if getattr(tool, "name", None) == SUBMIT_TOOL_NAME:
                session.submit_tool = tool
        return session


def _ctx_with(factory, tracer):
    from opencollab.application.workflow import WorkflowContext

    return WorkflowContext(factory, tracer=tracer, budget_total=None)


_DEAD_LEDGER = [
    {"tool": "grep", "target": "slice", "outcome": "hit", "snippet": "fs.py:42: end = start + n"},
    {"tool": "file_read", "target": "fs.py", "outcome": "duplicate", "snippet": "def slice(...)"},
]
_DEAD_MESSAGES = [
    {"role": "user", "content": "investigate"},
    {"role": "tool", "tool_call_id": "c1", "content": "fs.py:42: end = start + n"},
]


# --------------------------------------------------------------------------- #
# T2 — a dead/empty scout triggers the synthesizer -> cited findings.
# --------------------------------------------------------------------------- #


def test_t2_dead_scout_triggers_synthesizer_yielding_cited_findings():
    tracer = _FakeTracer()
    # Scout #1 dies without committing (captured None, empty reply) but left a ledger.
    dead = _FakeSession(capture=None, reply="", scout_ledger=list(_DEAD_LEDGER),
                        messages=list(_DEAD_MESSAGES))
    # Synthesizer session #2 commits a cited payload.
    synth = _FakeSession(capture=_cited_payload(), reply="")
    factory = _ScriptedFactory([dead, synth])
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

    # The vacuous dead-scout result was replaced by the synthesized cited findings.
    assert "root cause located" in result
    assert "fs.py:42" in result
    assert "scout died" not in result
    # A SECOND session (the synthesizer) was built, restricted to submit_findings only
    # with a forced tool_choice, and seeded with the ledger + transcript (NO read tools).
    assert len(factory.builds) == 2
    synth_build = factory.builds[1]
    assert [getattr(t, "name", None) for t in synth_build["tools"]] == [SUBMIT_TOOL_NAME]
    assert synth_build["tool_choice"] is not None  # forced (required / named-function)
    assert "fs.py:42" in synth_build["prompt"]  # the ledger evidence rode into the prompt
    # The synthesis was traced.
    assert any(s["step_type"] == "dead_scout_synthesis" for s in tracer.steps)


def test_t2_dead_scout_synthesizer_accepts_valid_insufficient_evidence():
    tracer = _FakeTracer()
    dead = _FakeSession(capture=None, reply="", scout_ledger=list(_DEAD_LEDGER),
                        messages=list(_DEAD_MESSAGES))
    synth = _FakeSession(capture=_insufficient_payload(), reply="")
    factory = _ScriptedFactory([dead, synth])
    ctx = _ctx_with(factory, tracer)

    result = run(
        ctx.agent(
            "scout the bug",
            tools=[_ReadStub()],
            label="scout:0",
            enforcement_strength=ENFORCEMENT_ON,
        )
    )

    # A valid insufficient_evidence abstention is a non-fabricated, acceptable outcome.
    assert "insufficient_evidence" in result
    assert len(factory.builds) == 2


def test_t2_committed_scout_does_not_trigger_synthesizer():
    # A scout that DID commit a structured payload is not dead — no synthesizer fires.
    tracer = _FakeTracer()
    committed = _FakeSession(capture=_cited_payload(), reply="", scout_ledger=list(_DEAD_LEDGER))
    factory = _ScriptedFactory([committed])
    ctx = _ctx_with(factory, tracer)

    result = run(
        ctx.agent("scout", tools=[_ReadStub()], label="scout:0", enforcement_strength=ENFORCEMENT_ON)
    )

    assert "root cause located" in result
    assert len(factory.builds) == 1  # only the scout, no synthesizer
    assert not any(s["step_type"] == "dead_scout_synthesis" for s in tracer.steps)


def test_t2_dead_scout_with_no_ledger_does_not_trigger_synthesizer():
    # Nothing was gathered (empty ledger AND no tool results) -> nothing to synthesize.
    tracer = _FakeTracer()
    dead = _FakeSession(capture=None, reply="", scout_ledger=[], messages=[])
    factory = _ScriptedFactory([dead])
    ctx = _ctx_with(factory, tracer)

    run(ctx.agent("scout", tools=[_ReadStub()], label="scout:0", enforcement_strength=ENFORCEMENT_ON))

    assert len(factory.builds) == 1  # no synthesizer session built


def test_t2_off_path_never_synthesizes():
    # enforcement off -> the unchanged _run_agent path; no synthesizer, no ledger read.
    tracer = _FakeTracer()
    session = _FakeSession(capture=None, reply="plain report", scout_ledger=list(_DEAD_LEDGER))
    factory = _ScriptedFactory([session])
    ctx = _ctx_with(factory, tracer)

    result = run(ctx.agent("scout", tools=[_ReadStub()], label="scout:0", enforcement_strength=ENFORCEMENT_OFF))

    assert result == "plain report"
    assert len(factory.builds) == 1
    assert not any(s["step_type"] == "dead_scout_synthesis" for s in tracer.steps)
