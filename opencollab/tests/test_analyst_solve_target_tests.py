"""analyst-solve weaves FAIL_TO_PASS node-ids into its prompts (inject-f2p).

The node-ids must reach the scope/plan/coder/tester prompts so the run is
scoped to the graded behavior. Anti-overfit: the block surfaces node-ids + a
root-cause instruction, never the tests' literal assertion values.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


class _FakeBudget:
    total = None

    def remaining(self) -> float:
        return float("inf")

    def spent(self) -> int:
        return 0


class ScriptedCtx:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.budget = _FakeBudget()

    async def agent(self, prompt, *, schema=None, label=None, tools=None, **kw):
        self.agent_calls.append({"prompt": prompt, "label": label})
        reply = self._replies.pop(0) if self._replies else None
        if isinstance(reply, dict) and tools:
            for tool in tools:
                if getattr(tool, "name", "") == "run_tests":
                    tool._verified_targets.update(reply.get("tests_run") or ())
        return reply

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def phase(self, title):
        pass

    async def log(self, message):
        pass

    async def tree_changed(self):
        return True  # something was edited -> no forced write

    async def source_changed(self, exclude_paths=()):
        return True  # source edited -> no forced write


DIMS = {"dimensions": [{"aspect": "bug", "question": "where?", "hints": []}]}
PLAN = {
    "root_cause": "rc",
    "approach": "ap",
    "phases": [{"goal": "g", "files": ["f.py"], "done": "behaves"}],
}
PASS = {"verdict": "PASS", "findings": ""}
# A PASS that clears the FAIL_TO_PASS gate: the required node-id ran with zero
# failures. Used wherever the scripted run must take the clean-PASS path.
PASS_F2P = {
    "verdict": "PASS",
    "findings": "",
    "tests_run": ["tests/test_widget.py::test_empty"],
    "failed_count": 0,
}


def test_node_ids_woven_into_prompts_without_literal_values():
    ctx = ScriptedCtx(
        replies=[DIMS, "scout findings", PLAN, "coded", PASS_F2P, PASS_F2P]
    )
    args = {
        "description": "fix the widget",
        "fail_to_pass": ["tests/test_widget.py::test_empty"],
    }
    asyncio.run(_run(ctx, args))

    prompts = "\n\n".join(c["prompt"] for c in ctx.agent_calls)
    # The node-id reached the prompts (scope/plan/coder/tester).
    assert prompts.count("tests/test_widget.py::test_empty") >= 2
    # Anti-overfit guardrail language is present.
    assert "ROOT CAUSE" in prompts
    assert "do not overfit" in prompts.lower() or "do NOT special-case" in prompts


def test_no_node_ids_leaves_prompts_unchanged_shape():
    ctx = ScriptedCtx(replies=[DIMS, "scout findings", PLAN, "coded", PASS, PASS])
    args = {"description": "fix the widget"}  # no fail_to_pass
    asyncio.run(_run(ctx, args))

    prompts = "\n\n".join(c["prompt"] for c in ctx.agent_calls)
    # With no ids threaded in, the target-tests block is absent entirely.
    assert "Target tests" not in prompts


async def _run(ctx, args):
    fn = discover_workflows(str(_WF_DIR)).get("analyst-solve").fn
    return await fn(ctx, args)
