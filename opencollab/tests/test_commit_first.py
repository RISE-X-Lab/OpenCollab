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
from pathlib import Path
from typing import Any

from opencollab.application.session_run import ENFORCEMENT_OFF, ENFORCEMENT_ON
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    harvest_findings,
)
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


def run(coro):
    return asyncio.run(coro)


def _recon_fn():
    fn = discover_workflows(str(_WF_DIR)).get("analyst-solve").fn
    return fn.__globals__["_recon"]


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


def test_harvest_refine_wins_over_draft():
    out = harvest_findings(_cited(summary="refined finding"), "", [], draft="DRAFT-ANCHORS")
    assert "refined finding" in out
    assert "DRAFT-ANCHORS" not in out


def test_harvest_falls_back_to_draft_when_no_capture():
    assert harvest_findings(None, "", [], draft="DRAFT-ANCHORS") == "DRAFT-ANCHORS"


def test_harvest_draft_ranks_above_prose_and_ledger():
    ledger = [{"tool": "grep", "target": "x", "outcome": "hit", "snippet": "y"}]
    out = harvest_findings(None, "vacuous prose", [], ledger=ledger, draft="DRAFT-ANCHORS")
    assert out == "DRAFT-ANCHORS"


def test_harvest_without_draft_is_unchanged():
    # draft defaults None -> byte-for-byte the prior priority (prose before partial).
    assert harvest_findings(None, "prose", []) == "prose"
    assert harvest_findings(None, "", []) == ""


# --------------------------------------------------------------------------- #
# draft_findings: bounded submit-only forced call returning the captured payload
# --------------------------------------------------------------------------- #


def test_draft_findings_returns_captured_payload_submit_only_forced():
    sess = _FakeSession(capture=_cited(summary="draft commit"), reply="")
    factory = _ScriptedFactory([sess])
    ctx = _ctx(factory)

    payload = run(ctx.draft_findings("draft this dimension", label="scout:0:bug:draft", budget=25_000))

    assert isinstance(payload, dict)
    assert payload["summary"] == "draft commit"
    build = factory.builds[0]
    assert [getattr(t, "name", None) for t in build["tools"]] == [SUBMIT_TOOL_NAME]  # submit-only
    assert build["tool_choice"] is not None  # forced (named-function)
    assert build["thinking"] is False  # exploration/reasoning off
    assert build["budget"] <= 25_000  # bounded


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
# _recon-level: drafts are committed before scouts + plumbed as fallback; off-parity
# --------------------------------------------------------------------------- #


class _Budget:
    total = 1_000_000

    def remaining(self) -> float:
        return 1_000_000.0

    def spent(self) -> int:
        return 0


class _ReconCtx:
    """_recon stand-in with a draft_findings that returns a fixed payload."""

    def __init__(self, *, workspace_root=None, with_drafts=True):
        self.workspace_root = workspace_root
        self.agent_calls: list[dict[str, Any]] = []
        self.draft_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.budget = _Budget()
        self._with_drafts = with_drafts
        if with_drafts:
            self.draft_findings = self._draft_findings  # only present when enabled

    async def _draft_findings(self, prompt, *, label=None, budget=None):
        self.draft_calls.append({"prompt": prompt, "label": label, "budget": budget})
        return _cited(summary=f"draft for {label}")

    async def agent(self, prompt, **kw):
        self.agent_calls.append({"prompt": prompt, **kw})
        return f"refined:{kw.get('label')}"

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def log(self, message):
        self.logs.append(message)


_DIMS = [
    {"aspect": "origin", "question": "where?", "hints": ["pkg/t.py"]},
    {"aspect": "contract", "question": "callers?", "hints": ["grep callers"]},
]


def _trivial_repo(tmp_path) -> tuple[str, str]:
    (tmp_path / "mod.py").write_text(
        'def tiny(a, b):\n    """Add a and b together for the widget subsystem."""\n'
        "    # TODO: implement this function\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    goal = (
        "TASK: Implement the function `tiny`.\n"
        f"- The function stub is at: {tmp_path / 'mod.py'} (near line 1)\n"
    )
    return str(tmp_path), goal


def _scout_calls(ctx: _ReconCtx):
    return [c for c in ctx.agent_calls if str(c.get("label", "")).startswith("scout:")]


def test_recon_commit_first_seeds_drafts_and_plumbs_fallback(tmp_path):
    recon = _recon_fn()
    root, goal = _trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, with_drafts=True)
    run(recon(ctx, goal, _DIMS, ENFORCEMENT_ON))

    scouts = _scout_calls(ctx)
    # A draft was committed for each surviving scout BEFORE exploring.
    assert len(ctx.draft_calls) == len(scouts)
    assert all(c["label"].endswith(":draft") for c in ctx.draft_calls)
    # Each scout prompt carries the committed draft + revise framing,
    # and the rendered draft is plumbed as that scout's harvest fallback.
    for c in scouts:
        assert "committed draft" in c["prompt"]
        assert "submit_findings" in c["prompt"]
        assert isinstance(c["harvest_fallback"], str) and c["harvest_fallback"].strip()
        # The draft prompt is fact-sheet-derived; its summary rode into the fallback.
        assert "draft for" in c["harvest_fallback"]


def test_recon_off_makes_no_draft_call(tmp_path):
    recon = _recon_fn()
    root, goal = _trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, with_drafts=True)
    run(recon(ctx, goal, _DIMS, ENFORCEMENT_OFF))

    # OFF: no drafting, no fact sheet, harvest_fallback inert, prompts unchanged.
    assert ctx.draft_calls == []
    scouts = _scout_calls(ctx)
    assert len(scouts) == len(_DIMS)
    assert all(c.get("harvest_fallback") is None for c in scouts)
    assert all("committed draft" not in c["prompt"] for c in scouts)
    assert all("Pre-recon fact sheet" not in c["prompt"] for c in scouts)


def test_recon_on_without_draft_findings_degrades_gracefully(tmp_path):
    # enforcement on + manifest, but a ctx WITHOUT draft_findings -> 5a fact sheet
    # still injected, commit-first simply inert (no drafts, no fallback).
    recon = _recon_fn()
    root, goal = _trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, with_drafts=False)
    run(recon(ctx, goal, _DIMS, ENFORCEMENT_ON))

    scouts = _scout_calls(ctx)
    assert ctx.draft_calls == []
    assert all(c.get("harvest_fallback") is None for c in scouts)
    assert all("committed draft" not in c["prompt"] for c in scouts)
    # 5a still active: the fact sheet rode into the hints.
    assert any("Pre-recon fact sheet" in c["prompt"] for c in scouts)
