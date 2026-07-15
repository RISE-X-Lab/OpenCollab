"""STEP 5b — commit-first turn-0 draft (Design B: bounded pre-pass, no FSM changes).

Each scout commits a DRAFT findings artifact from the static fact sheet BEFORE it
explores; it then runs the unchanged capture→cancel→harvest path, revising the draft
into its own refined submit (which is what gets harvested). The draft is also the
per-scout HARVEST FALLBACK so a scout that dies/strays before refining never loses the
fact-sheet anchors.

* T1 — with enforcement on + a manifest, each scout is seeded a draft before exploring,
  and the harvested report is the scout's REFINED submit (not the draft) when it refines.
* T2 — a scout that dies before refining falls back to the draft (anchors preserved);
  a dead scout WITH real reads (ledger) still prefers the grounded synth over the draft.
* off==reference — enforcement off => no draft call, harvest_fallback ignored, scout
  flow byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio

from opencollab.application.session_run import ENFORCEMENT_OFF, ENFORCEMENT_ON
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
)
from opencollab.application.workflow import WorkflowContext


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# scripted workflow-engine harness (mirrors test_evidence_ledger)
# --------------------------------------------------------------------------- #


def _cited(summary="root cause located", anchor="fs.py:42"):
    return {
        "findings": [
            {
                "aspect": "bug",
                "claim": "off-by-one",
                "evidence_anchor": anchor,
                "verified": True,
                "confidence": "high",
            }
        ],
        "summary": summary,
        "insufficient_evidence": False,
    }


class _ReadStub:
    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


class _FakeState:
    def __init__(self, used_tokens=0, scout_ledger=None, messages=None):
        self.used_tokens = used_tokens
        self.wind_down_done = False
        self.wind_down_token_mark = 0
        self.messages = messages if messages is not None else []
        self.scout_ledger = scout_ledger if scout_ledger is not None else []


class _FakeRunner:
    def configure_enforcement(self, **kw):
        pass


class _FakeSession:
    def __init__(self, *, capture=None, reply="", scout_ledger=None, messages=None,
                 max_budget_tokens=100_000):
        self.runner = _FakeRunner()
        self.state = _FakeState(scout_ledger=scout_ledger, messages=messages)
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
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.builds = []

    def build_workflow_session(self, *, prompt, budget, tools=None, isolation=False,
                               label=None, tool_choice=None, thinking=None):
        self.builds.append(
            {"tools": list(tools or []), "label": label, "tool_choice": tool_choice,
             "thinking": thinking, "prompt": prompt, "budget": budget}
        )
        session = self.sessions.pop(0)
        for tool in tools or []:
            if getattr(tool, "name", None) == SUBMIT_TOOL_NAME:
                session.submit_tool = tool
        return session


def _ctx(factory):
    return WorkflowContext(factory, budget_total=None)


# --------------------------------------------------------------------------- #
# harvest priority (unit): captured > draft > prose/ledger; no-draft unchanged
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# draft_findings: bounded submit-only forced call returning the captured payload
# --------------------------------------------------------------------------- #


def test_draft_findings_none_when_no_capture():
    sess = _FakeSession(capture=None, reply="")
    ctx = _ctx(_ScriptedFactory([sess]))
    assert run(ctx.draft_findings("draft", label="d", budget=25_000)) is None


# --------------------------------------------------------------------------- #
# T1/T2 — harvest_fallback plumbing through the enforced scout path
# --------------------------------------------------------------------------- #


def test_t1_enforced_scout_harvests_refine_not_draft():
    # The scout commits its OWN refined submit -> that wins over the seeded draft.
    scout = _FakeSession(capture=_cited(summary="scout refined this"), reply="")
    ctx = _ctx(_ScriptedFactory([scout]))
    out = run(
        ctx.agent(
            "scout", tools=[_ReadStub()], label="scout:0",
            enforcement_strength=ENFORCEMENT_ON, harvest_fallback="DRAFT-ANCHORS",
        )
    )
    assert "scout refined this" in out
    assert "DRAFT-ANCHORS" not in out


def test_t2_dead_scout_falls_back_to_draft():
    # No own capture, no reads (empty ledger) -> the draft is the salvage.
    dead = _FakeSession(capture=None, reply="", scout_ledger=[], messages=[])
    ctx = _ctx(_ScriptedFactory([dead]))
    out = run(
        ctx.agent(
            "scout", tools=[_ReadStub()], label="scout:0",
            enforcement_strength=ENFORCEMENT_ON, harvest_fallback="DRAFT-ANCHORS",
        )
    )
    assert out == "DRAFT-ANCHORS"


def test_t2_dead_scout_with_reads_prefers_grounded_synth_over_draft():
    # A dead scout that DID read (ledger) -> the grounded synth overrides the draft.
    ledger = [{"tool": "grep", "target": "slice", "outcome": "hit", "snippet": "fs.py:42"}]
    dead = _FakeSession(capture=None, reply="", scout_ledger=list(ledger),
                        messages=[{"role": "tool", "tool_call_id": "c1", "content": "fs.py:42"}])
    synth = _FakeSession(capture=_cited(summary="grounded synth"), reply="")
    ctx = _ctx(_ScriptedFactory([dead, synth]))
    out = run(
        ctx.agent(
            "scout", tools=[_ReadStub()], label="scout:0",
            enforcement_strength=ENFORCEMENT_ON, harvest_fallback="DRAFT-ANCHORS",
        )
    )
    assert "grounded synth" in out
    assert "DRAFT-ANCHORS" not in out


def test_off_path_ignores_harvest_fallback():
    # enforcement off -> the unchanged _run_agent path; harvest_fallback is inert.
    sess = _FakeSession(capture=None, reply="plain reply")
    ctx = _ctx(_ScriptedFactory([sess]))
    out = run(
        ctx.agent(
            "scout", tools=[_ReadStub()], label="scout:0",
            enforcement_strength=ENFORCEMENT_OFF, harvest_fallback="DRAFT-ANCHORS",
        )
    )
    assert out == "plain reply"


# --------------------------------------------------------------------------- #
