"""analyst-solve hard-gates the tester verdict on the real FAIL_TO_PASS tests
(tester-real-pass).

A tester PASS is only trusted when it carries machine-checkable proof the named
FAIL_TO_PASS node-ids actually ran green: ``failed_count == 0`` AND every
required node-id present in ``tests_run``. Otherwise ``_run_phase`` overrides the
PASS to not-passed and seeds the next round's findings — mirroring the existing
tree-unchanged diff guard. The gate is conditional (D2): it fires only when
FAIL_TO_PASS ids were injected; with an empty list the verdict stands as today.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"

F2P = ["tests/test_widget.py::test_empty"]


class _FakeBudget:
    total = None

    def remaining(self) -> float:
        return float("inf")

    def spent(self) -> int:
        return 0


class ScriptedCtx:
    """WorkflowContext stand-in scripting agent() replies; tree_changed is fixed."""

    def __init__(self, replies: list[Any], *, tree: bool | None = True) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()
        self._tree = tree

    async def agent(self, prompt, *, schema=None, label=None, tools=None, **kw):
        self.agent_calls.append({"prompt": prompt, "label": label, "schema": schema})
        return self._replies.pop(0) if self._replies else None

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def phase(self, title):
        pass

    async def log(self, message):
        self.logs.append(message)

    async def tree_changed(self):
        return self._tree


DIMS = {"dimensions": [{"aspect": "bug", "question": "where?", "hints": []}]}
PLAN = {
    "root_cause": "rc",
    "approach": "ap",
    "phases": [{"goal": "g", "files": ["f.py"], "done": "behaves"}],
}


def _pass(*, tests_run, failed_count, findings="") -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "findings": findings,
        "tests_run": list(tests_run),
        "failed_count": failed_count,
    }


def _fail(findings: str) -> dict[str, Any]:
    return {"verdict": "FAIL", "findings": findings, "tests_run": [], "failed_count": 1}


def _wf_fn():
    return discover_workflows(str(_WF_DIR)).get("analyst-solve").fn


def _f2p_gate():
    # Reuse the exact module the workflow runs in — its globals carry _f2p_gate.
    return _wf_fn().__globals__["_f2p_gate"]


async def _run(ctx, args):
    return await _wf_fn()(ctx, args)


def _coder_prompts(ctx) -> list[str]:
    return [c["prompt"] for c in ctx.agent_calls if (c["label"] or "").startswith("coder:")]


# (a) tester verdict PASS but failed_count=1 -> _run_phase overrides + seeds findings.
def test_pass_with_failed_count_is_overridden_and_seeds_findings():
    # Round 1: clean-looking PASS but failed_count=1 -> overridden. Round 2: a
    # genuine clean PASS stands. The phase ends "passed", but only after the gate
    # forced a second round carrying the failure findings.
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "coded r1",
            _pass(tests_run=F2P, failed_count=1),  # overridden
            "coded r2",
            _pass(tests_run=F2P, failed_count=0),  # stands
            _pass(tests_run=F2P, failed_count=0),  # final verify
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    assert result["phases"][0]["status"] == "passed"
    assert result["phases"][0]["rounds"] == 2
    # The override seeded the next coder round with the failed-count findings.
    coder_r2 = _coder_prompts(ctx)[1]
    assert "failed/errored test" in coder_r2
    assert any("FAIL_TO_PASS proof insufficient" in m for m in ctx.logs)


# (b) tester PASS with tests_run missing a required node-id -> overridden.
def test_pass_missing_required_node_id_is_overridden():
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "coded r1",
            _pass(tests_run=["tests/test_other.py::test_x"], failed_count=0),  # missing F2P id
            "coded r2",
            _pass(tests_run=F2P, failed_count=0),  # stands
            _pass(tests_run=F2P, failed_count=0),  # final verify
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    assert result["phases"][0]["status"] == "passed"
    assert result["phases"][0]["rounds"] == 2
    coder_r2 = _coder_prompts(ctx)[1]
    assert "tests/test_widget.py::test_empty" in coder_r2
    assert "not shown as executed" in coder_r2


# (b') gate persists to the final round -> phase ends "failed" with findings.
def test_gate_failure_on_final_round_marks_phase_failed():
    # Every round returns a PASS that fails the gate (missing the node-id). After
    # MAX_ROUNDS_PER_PHASE the phase is "failed", not "passed".
    bad = _pass(tests_run=[], failed_count=0)
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "c1", bad,
            "c2", bad,
            "c3", bad,
            "c4", bad,
            # final verify also fails the gate
            _pass(tests_run=[], failed_count=0),
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    assert result["phases"][0]["status"] == "failed"
    assert result["phases"][0]["rounds"] == 4
    assert "not shown as executed" in result["phases"][0]["last_findings"]


# (c) tester PASS, failed_count=0, all node-ids present -> stands first round.
def test_clean_pass_with_all_node_ids_stands():
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "coded",
            _pass(tests_run=F2P, failed_count=0),  # phase PASS stands
            _pass(tests_run=F2P, failed_count=0),  # final verify
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    assert result["phases"][0]["status"] == "passed"
    assert result["phases"][0]["rounds"] == 1
    assert not any("proof insufficient" in m for m in ctx.logs)


# (d) fail_to_pass empty -> gate bypassed, today's behavior preserved.
def test_empty_fail_to_pass_bypasses_the_gate():
    # A bare PASS (no proof fields) would FAIL the gate if it ran — but with no
    # ids injected the gate is skipped and the PASS stands on round 1.
    bare_pass = {"verdict": "PASS", "findings": ""}
    ctx = ScriptedCtx(
        replies=[DIMS, "scout", PLAN, "coded", bare_pass, bare_pass],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget"}))  # no fail_to_pass

    assert result["phases"][0]["status"] == "passed"
    assert result["phases"][0]["rounds"] == 1
    assert result["status"] == "done"
    assert not any("proof insufficient" in m for m in ctx.logs)


# (e) final status: verified True only when the f2p gate passes.
def test_final_verified_requires_f2p_gate_to_pass():
    # The phase passes cleanly, but the FINAL verify returns a PASS that fails
    # the gate (missing the node-id). verified must be False -> final_verdict is
    # not trusted as a real PASS. Status still "done" via passed_phases, but the
    # final_verdict did not certify it.
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "coded",
            _pass(tests_run=F2P, failed_count=0),  # phase PASS stands
            _pass(tests_run=[], failed_count=0),  # final verify FAILS the gate
            # repair round is only entered on verdict==FAIL, not on a gate-failed
            # PASS, so no further replies are consumed here.
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    # final verdict labelled PASS but missing the node-id -> not "verified".
    assert _f2p_gate()(result["final_verdict"], F2P) is not None
    # phase passed, so the run is still "done" via passed_phases, but NOT because
    # the final verdict certified it.
    assert result["phases_passed"] == result["phases_planned"]


def test_final_verified_true_when_gate_clears():
    ctx = ScriptedCtx(
        replies=[
            DIMS,
            "scout",
            PLAN,
            "coded",
            _pass(tests_run=F2P, failed_count=0),  # phase PASS
            _pass(tests_run=F2P, failed_count=0),  # final verify clears the gate
        ],
        tree=True,
    )
    result = asyncio.run(_run(ctx, {"description": "fix the widget", "fail_to_pass": F2P}))

    assert _f2p_gate()(result["final_verdict"], F2P) is None
    assert result["status"] == "done"
