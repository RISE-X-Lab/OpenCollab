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
    assert outcome["prompt_revision"] == 5
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_g22_identical_candidates_keep_mechanical_selection():
    ctx = Context(identical=True)
    outcome = await g22.duo(ctx, {"goal": "Repair public behavior"})
    assert outcome["prompt_revision"] == 5
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
    assert _prompts._PROMPT_REVISION == 5


@pytest.mark.parametrize("shared_gap", ["not_covered", "unclear"])
async def test_complete_inventory_with_shared_shortcoming_can_select_b(tmp_path, shared_gap):
    result = decision()
    result["requirements"].append({
        "requirement": "Preserve an edge case that both candidates leave unresolved",
        "a_coverage": shared_gap,
        "b_coverage": shared_gap,
        "a_evidence": ["src/handler.py does not establish this edge case"],
        "b_evidence": ["src/handler.py does not establish this edge case"],
    })
    ctx = Context(result=result)
    outcome = await g22.duo(
        ctx, {"goal": "Handle normal input and the edge case", "candidate_evidence_dir": str(tmp_path)},
    )
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "contract-adjudicated"
    prompt, options = ctx.selector_calls[0]
    assert "requirement inventory, not candidate correctness" in prompt
    description = options["schema"]["properties"]["requirements_complete"]["description"]
    assert "inventory completeness, not candidate correctness" in description
    assert "not_covered or unclear" in description


async def test_incomplete_inventory_still_rejects_a_b_recommendation(tmp_path):
    result = decision()
    result["requirements_complete"] = False
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {"goal": "Cover all requested behavior", "candidate_evidence_dir": str(tmp_path)})
    assert outcome["judge_result"] == result
    assert outcome["winner"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


async def test_working_tree_delivery_keeps_a_better_supported_requirement_protected(tmp_path):
    result = decision()
    result["requirements"].append({
        "requirement": "Preserve the existing default behavior",
        "a_coverage": "covered",
        "b_coverage": "not_covered",
        "a_evidence": ["src/handler.py keeps the default path"],
        "b_evidence": ["src/handler.py removes the default path"],
    })
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Implement the return value and preserve defaults",
        "submission_mode": "working_tree",
        "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"]["winner"] == "B"
    assert outcome["winner"] == outcome["adopted"] == "A"


async def test_submission_mode_is_consistent_across_roles_and_isolated_between_calls(tmp_path):
    goal = "Repair the return value and commit the change"
    delegated, task_owned = Context(), Context()
    outcomes = await asyncio.gather(
        g22.duo(delegated, {"goal": goal, "submission_mode": "working_tree", "candidate_evidence_dir": str(tmp_path)}),
        g22.duo(task_owned, {"goal": goal, "candidate_evidence_dir": str(tmp_path)}),
    )
    assert [r["submission_mode"] for r in outcomes] == ["working_tree", "task"]
    for prompt, _ in [*delegated.coder_calls, *delegated.selector_calls]:
        assert goal in prompt
        assert "Runtime submission mode: working_tree" in prompt
        assert "caller after selection" in prompt
        assert "absent candidate commit" in prompt
    for prompt, _ in [*task_owned.coder_calls, *task_owned.selector_calls]:
        assert goal in prompt
        assert "Runtime submission mode: working_tree" not in prompt


@pytest.mark.parametrize("mode", ["unknown", None, {}, True])
async def test_invalid_submission_mode_is_rejected_before_candidate_execution(mode):
    ctx = Context()
    with pytest.raises(ValueError, match="submission_mode"):
        await g22.duo(ctx, {"goal": "Repair behavior", "submission_mode": mode})
    assert ctx.coder_calls == []
