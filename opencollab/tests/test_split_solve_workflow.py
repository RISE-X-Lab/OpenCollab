"""Tests for the ``split-solve`` workflow (workflows/split_solve.py).

The workflow is pure orchestration over ``ctx.agent``, so a scripted context
stand-in (same pattern as ``test_evaluator_workflow.ScriptedCtx``) exercises
every control-flow guarantee without touching an LLM:

* analyze -> per-subtask coder/tester loop -> synthesize, in order;
* a failed subtask never blocks the remaining ones (unlike self-collab phases);
* retry rounds always carry the tester's findings (never-identical retries);
* synthesis runs only when at least one subtask passed;
* dead agents (None replies) degrade locally, never abort the workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


@pytest.fixture(scope="module")
def split_solve():
    registry = discover_workflows(str(_WF_DIR))
    return registry.get("split-solve").fn


class _FakeBudget:
    total = None

    def spent(self) -> int:
        return 42


class ScriptedCtx:
    """Minimal WorkflowContext stand-in scripting agent() replies in order."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append({"prompt": prompt, "schema": schema, "label": label})
        return self._replies.pop(0)

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)


PLAN_ONE = {
    "root_cause": "off-by-one in pager",
    "subtasks": [
        {"goal": "fix pager bounds", "files": ["pager.py"], "done": "test_pager passes"},
    ],
}

PLAN_TWO = {
    "root_cause": "off-by-one in pager",
    "subtasks": [
        {"goal": "fix pager bounds", "files": ["pager.py"], "done": "test_pager passes"},
        {"goal": "fix docs example", "files": ["docs.py"], "done": "doctest passes"},
    ],
}

PASS = {"verdict": "PASS", "findings": ""}
CLEAN = {"status": "clean", "summary": "all good", "remaining_issues": ""}


def fail(findings: str) -> dict[str, str]:
    return {"verdict": "FAIL", "findings": findings}


def blocked(findings: str) -> dict[str, str]:
    return {"verdict": "BLOCKED", "findings": findings}


async def test_happy_path_all_subtasks_pass(split_solve):
    ctx = ScriptedCtx([PLAN_TWO, "coder report 1", PASS, "coder report 2", PASS, CLEAN])

    result = await split_solve(ctx, {"goal": "fix the pager"})

    assert result["status"] == "done"
    assert ctx.phases == ["analyze", "solve", "synthesize"]
    # analyst + 2 x (coder, tester) + synthesizer
    assert len(ctx.agent_calls) == 6
    assert [c["label"] for c in ctx.agent_calls] == [
        "analyst",
        "coder:s0r1",
        "tester:s0r1",
        "coder:s1r1",
        "tester:s1r1",
        "synthesizer",
    ]
    assert all(r["status"] == "passed" and r["rounds"] == 1 for r in result["subtasks"])
    assert result["subtasks_planned"] == 2
    assert result["synthesis"] == CLEAN
    assert result["root_cause"] == "off-by-one in pager"
    assert result["tokens_spent"] == 42


async def test_failed_subtask_does_not_block_the_rest(split_solve):
    ctx = ScriptedCtx(
        [
            PLAN_TWO,
            "c s0r1", fail("boom1"),
            "c s0r2", fail("boom2"),
            "c s0r3", fail("boom3"),
            "c s1r1", PASS,
            {"status": "issues", "summary": "subtask 0 broken", "remaining_issues": "pager still off"},
        ]
    )

    result = await split_solve(ctx, {"goal": "fix the pager"})

    # Subtask 1 still ran after subtask 0 exhausted its rounds.
    assert len(ctx.agent_calls) == 10
    assert result["status"] == "incomplete"
    assert result["subtasks"][0] == {
        "goal": "fix pager bounds",
        "status": "failed",
        "rounds": 3,
        "last_findings": "boom3",
    }
    assert result["subtasks"][1]["status"] == "passed"
    # Never-identical retries: round 2's coder prompt carries round 1's findings.
    assert "boom1" in ctx.agent_calls[3]["prompt"]
    assert "boom2" in ctx.agent_calls[5]["prompt"]
    # The synthesizer sees the failed subtask's last findings in its report dump.
    assert "boom3" in ctx.agent_calls[9]["prompt"]


async def test_blocked_verdict_short_circuits_the_round_loop(split_solve):
    # One coder + one tester, then the tester returns BLOCKED on round 1. The
    # loop must stop immediately instead of burning rounds 2 and 3, so the next
    # scripted reply (here a stray PASS) is never consumed.
    ctx = ScriptedCtx([PLAN_ONE, "c r1", blocked("ModuleNotFoundError: no numpy"), PASS, CLEAN])

    result = await split_solve(ctx, {"goal": "fix the pager"})

    # analyst + exactly ONE (coder, tester) round — rounds 2 and 3 never ran.
    assert len(ctx.agent_calls) == 3
    assert [c["label"] for c in ctx.agent_calls] == ["analyst", "coder:s0r1", "tester:s0r1"]
    assert result["subtasks"][0] == {
        "goal": "fix pager bounds",
        "status": "blocked",
        "rounds": 1,
        "blocker": "ModuleNotFoundError: no numpy",
    }
    # No subtask passed -> synthesis is skipped, status is incomplete.
    assert result["status"] == "incomplete"
    assert result["synthesis"] is None
    assert ctx.phases == ["analyze", "solve"]
    assert any("BLOCKED" in m for m in ctx.logs)


async def test_synthesis_skipped_when_no_subtask_passed(split_solve):
    ctx = ScriptedCtx(
        [PLAN_ONE, "c r1", fail("e1"), "c r2", fail("e2"), "c r3", fail("e3")]
    )

    result = await split_solve(ctx, {"goal": "fix the pager"})

    # analyst + 3 x (coder, tester), no synthesizer call
    assert len(ctx.agent_calls) == 7
    assert result["status"] == "incomplete"
    assert result["synthesis"] is None
    assert ctx.phases == ["analyze", "solve"]
    assert any("skipping synthesis" in m for m in ctx.logs)


async def test_dead_tester_substitutes_findings_and_loop_continues(split_solve):
    ctx = ScriptedCtx([PLAN_ONE, "c r1", None, "c r2", PASS, CLEAN])

    result = await split_solve(ctx, {"goal": "fix the pager"})

    assert result["status"] == "done"
    assert result["subtasks"][0]["rounds"] == 2
    # The substituted findings keep round 2's prompt non-identical to round 1's.
    assert "Re-verify the definition of done" in ctx.agent_calls[3]["prompt"]


async def test_no_usable_plan_is_an_error(split_solve):
    ctx = ScriptedCtx([None])

    result = await split_solve(ctx, {"goal": "fix the pager"})

    assert result["status"] == "error"
    assert len(ctx.agent_calls) == 1


async def test_missing_goal_is_an_error_before_any_agent(split_solve):
    ctx = ScriptedCtx([])

    result = await split_solve(ctx, {})

    assert result["status"] == "error"
    assert ctx.agent_calls == []


async def test_description_arg_is_accepted_for_harness_runs(split_solve):
    ctx = ScriptedCtx([PLAN_ONE, "c", PASS, CLEAN])

    result = await split_solve(ctx, {"description": "fix it from swebench"})

    assert result["status"] == "done"
    assert "fix it from swebench" in ctx.agent_calls[0]["prompt"]


def test_discovery_registers_both_workflows():
    registry = discover_workflows(str(_WF_DIR))
    names = [s.name for s in registry.list_specs()]
    assert "split-solve" in names
    assert "self-collab" in names
