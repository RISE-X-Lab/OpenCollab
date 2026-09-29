"""Exercise Duo selection and per-call selector isolation."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from opencollab.builtin_workflows import _prompts, _selection
from tests.support.duo_test_support import (
    Context,
    decision,
)

g22 = importlib.import_module("opencollab.builtin_workflows.duo")

WORKFLOW = "duo"


def coverage_decision(
    *, winner, winner_coverage="covered", loser_coverage="unclear",
    winner_evidence="src/handler.py implements the required return value",
):
    result = decision()
    result["winner"] = winner
    requirement = result["requirements"][0]
    loser = "B" if winner == "A" else "A"
    requirement[f"{winner.lower()}_coverage"] = winner_coverage
    requirement[f"{loser.lower()}_coverage"] = loser_coverage
    requirement[f"{winner.lower()}_evidence"] = [winner_evidence] if winner_evidence else []
    return result


def add_unsupported_covered_requirement(result, *, winner, loser_coverage):
    loser = "B" if winner == "A" else "A"
    result["requirements"].append({
        "requirement": "Preserve the secondary return value",
        f"{winner.lower()}_coverage": "covered",
        f"{loser.lower()}_coverage": loser_coverage,
        f"{winner.lower()}_evidence": [],
        f"{loser.lower()}_evidence": [],
    })


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("loser_coverage", ["not_covered", "unclear"])
def test_covered_requirement_with_changed_path_evidence_outweighs_gap(winner, loser_coverage):
    result = coverage_decision(winner=winner, loser_coverage=loser_coverage)
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._validated_judge_winner(result, paths) == winner


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("unsupported_gap", ["unclear", "not_covered"])
@pytest.mark.parametrize("unsupported_first", [False, True])
def test_unsupported_extra_requirement_preserves_only_existing_valid_advantage(
    winner, unsupported_gap, unsupported_first,
):
    result = coverage_decision(winner=winner, loser_coverage="not_covered")
    add_unsupported_covered_requirement(result, winner=winner, loser_coverage=unsupported_gap)
    if unsupported_first:
        result["requirements"].reverse()
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    expected = winner if unsupported_gap == "unclear" else None
    assert _selection._validated_judge_winner(result, paths) == expected


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("evidence", ["", "docs/overview.md describes the return value"])
def test_coverage_advantage_requires_winner_changed_path_evidence(winner, evidence):
    result = coverage_decision(winner=winner, winner_evidence=evidence)
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._validated_judge_winner(result, paths) is None


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("coverage", ["unclear", "covered"])
def test_equal_coverage_gives_no_evidence_advantage(winner, coverage):
    result = coverage_decision(winner=winner, winner_coverage=coverage, loser_coverage=coverage)
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._validated_judge_winner(result, paths) is None


@pytest.mark.parametrize("winner", ["A", "B"])
def test_explicit_winner_gap_overrides_another_supported_advantage(winner):
    result = coverage_decision(winner=winner)
    loser = "B" if winner == "A" else "A"
    result["requirements"].append({
        "requirement": "Preserve the existing default behavior",
        f"{winner.lower()}_coverage": "not_covered",
        f"{loser.lower()}_coverage": "covered",
        f"{winner.lower()}_evidence": ["src/handler.py removes the default path"],
        f"{loser.lower()}_evidence": ["src/handler.py keeps the default path"],
    })
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._validated_judge_winner(result, paths) is None


@pytest.mark.parametrize("winner", ["A", "B"])
def test_incomplete_requirement_inventory_rejects_supported_advantage(winner):
    result = coverage_decision(winner=winner)
    result["requirements_complete"] = False
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._validated_judge_winner(result, paths) is None


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("loser_coverage", ["not_covered", "unclear"])
async def test_duo_adopts_supported_covered_candidate(tmp_path, winner, loser_coverage):
    result = coverage_decision(winner=winner, loser_coverage=loser_coverage)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == winner
    assert outcome["selection_reason"] == "contract-adjudicated"
    assert ctx.adoptions[0][0].label == f"dual-coder-contract-{winner.lower()}"


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("unsupported_gap", ["unclear", "not_covered"])
async def test_duo_distinguishes_extra_unsupported_uncertainty_from_explicit_gap(
    tmp_path, winner, unsupported_gap,
):
    result = coverage_decision(winner=winner, loser_coverage="not_covered")
    add_unsupported_covered_requirement(result, winner=winner, loser_coverage=unsupported_gap)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve both return values", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    expected_winner = winner if unsupported_gap == "unclear" else "A"
    expected_reason = (
        "contract-adjudicated" if unsupported_gap == "unclear"
        else "contract-evidence-insufficient-default-a"
    )
    assert outcome["winner"] == outcome["adopted"] == expected_winner
    assert outcome["selection_reason"] == expected_reason


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("coverage", ["unclear", "covered"])
async def test_duo_defaults_to_a_without_coverage_advantage(tmp_path, winner, coverage):
    result = coverage_decision(winner=winner, winner_coverage=coverage, loser_coverage=coverage)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("evidence", ["", "docs/overview.md describes the return value"])
async def test_duo_rejects_unsupported_coverage_advantage(tmp_path, winner, evidence):
    result = coverage_decision(winner=winner, winner_evidence=evidence)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


async def test_duo_rejects_winner_with_explicit_gap_despite_supported_advantage(tmp_path):
    result = coverage_decision(winner="B")
    result["requirements"].append({
        "requirement": "Preserve the existing default behavior",
        "a_coverage": "covered",
        "b_coverage": "not_covered",
        "a_evidence": ["src/handler.py keeps the default path"],
        "b_evidence": ["src/handler.py removes the default path"],
    })
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the return value and defaults", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_g22_preserves_original_rejection_and_default_a(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    result = decision("The candidate implements the requirement")
    ctx = Context(result=result)
    outcome = await g22.duo(
        ctx, {"goal": "Preserve the public return value"},
    )
    assert outcome["judge_result"] == result
    assert outcome["prompt_revision"] == 6
    assert outcome["winner"] == outcome["adopted"] == "A"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_g22_identical_candidates_keep_mechanical_selection():
    ctx = Context(identical=True)
    outcome = await g22.duo(ctx, {"goal": "Repair public behavior"})
    assert outcome["prompt_revision"] == 6
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
    assert _prompts._PROMPT_REVISION == 6


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
