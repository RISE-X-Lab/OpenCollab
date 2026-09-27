"""Exercise Duo selection and per-call selector isolation."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from opencollab.builtin_workflows import _prompts
from opencollab.workflows import CandidateRun

g22 = importlib.import_module("opencollab.builtin_workflows.duo")

WORKFLOW = "duo"


def candidate(label, value):
    return CandidateRun(
        label=label,
        output="Public repair completed",
        diff=("diff --git a/src/handler.py b/src/handler.py\n"
              "--- a/src/handler.py\n+++ b/src/handler.py\n"
              f"@@ -1 +1 @@\n-old\n+{value}\n"),
        test_records=(),
        verified_targets=(),
    )


def decision(evidence="src/handler.py implements the required return value"):
    return {
        "winner": "B",
        "requirements_complete": True,
        "requirements": [{
            "requirement": "Preserve the public return value",
            "a_coverage": "not_covered",
            "b_coverage": "covered",
            "a_evidence": ["src/handler.py retains the incorrect value"],
            "b_evidence": [evidence],
        }],
        "rationale": "B covers the requirement that A leaves unresolved",
    }


class Context:
    def __init__(self, *, result=None, barrier=None, identical=False):
        self.result = result or decision()
        self.barrier = barrier
        self.identical = identical
        self.coder_calls = []
        self.selector_calls = []
        self.adoptions = []
        self.phases = []

    async def candidate_agent(self, prompt, **options):
        if not self.coder_calls and self.barrier is not None:
            await self.barrier()
        self.coder_calls.append((prompt, options))
        value = "a" if self.identical or len(self.coder_calls) == 1 else "b"
        return candidate(options["label"], value)

    async def agent(self, prompt, **options):
        self.selector_calls.append((prompt, options))
        return self.result

    async def phase(self, title):
        self.phases.append(title)

    async def diff(self):
        return "[Working tree status]\n(clean)"

    async def adopt_candidate(self, selected, *, preserve_paths):
        self.adoptions.append((selected, preserve_paths))

    async def log(self, message):
        pass

    def tokens_spent(self):
        return 0






@pytest.mark.asyncio
async def test_g22_preserves_original_rejection_and_default_a(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    result = decision("The candidate implements the requirement")
    ctx = Context(result=result)
    outcome = await g22.duo(
        ctx, {"goal": "Preserve the public return value"},
    )
    assert outcome["judge_result"] == result
    assert outcome["prompt_revision"] == 4
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_g22_identical_candidates_keep_mechanical_selection():
    ctx = Context(identical=True)
    outcome = await g22.duo(ctx, {"goal": "Repair public behavior"})
    assert outcome["prompt_revision"] == 4
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "identical-diff"
    assert outcome["judge_used"] is False and not ctx.selector_calls


async def test_parallel_duo_calls_keep_task_and_evidence_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    ready = asyncio.Event()
    entered = 0

    async def barrier():
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()

    contexts = [Context(barrier=barrier), Context(barrier=barrier)]
    goals = ["Write a CSV report", "Configure a service"]
    results = await asyncio.wait_for(asyncio.gather(*[
        g22.duo(ctx, {"goal": goal, "candidate_evidence_dir": str(tmp_path)})
        for ctx, goal in zip(contexts, goals)
    ]), timeout=2)
    directories = []
    for ctx, goal, other, result in zip(contexts, goals, reversed(goals), results):
        assert result["winner"] == result["adopted"] == "B"
        assert all(goal in prompt and other not in prompt for prompt, _ in ctx.coder_calls)
        assert all(options["budget"] is None for _, options in ctx.coder_calls)
        prompt, options = ctx.selector_calls[0]
        assert goal in prompt and other not in prompt
        assert options["budget"] is None
        tool, = options["tools"]
        assert tool.name == "read_candidate_evidence"
        directories.append(tool.files.directory)
    assert directories[0] != directories[1]


@pytest.mark.parametrize("task", [
    "Update the package configuration and requested snapshots",
    "Produce the requested CSV and image files",
    "Configure the application and leave its service running",
])
async def test_task_oriented_prompts_preserve_the_complete_delivery_scope(task):
    ctx = Context()
    await g22.duo(ctx, {"goal": task})
    for prompt, _ in ctx.coder_calls:
        assert task in prompt
        assert "configuration, dependencies" in prompt
        assert "artifacts and services" in prompt
        assert "Update public" in prompt
        assert "non-empty source diff" not in prompt
        assert "Do not run git commit" not in prompt
        assert "withheld reference answers" in prompt
    assert _prompts._PROMPT_REVISION == 4
