"""Exercise Duo selection and per-call selector isolation."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from opencollab.builtin_workflows import _prompts
from tests.support.duo_test_support import (
    Context,
    decision,
)

g22 = importlib.import_module("opencollab.builtin_workflows.duo")

WORKFLOW = "duo"


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
