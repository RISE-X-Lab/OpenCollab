"""Exercise Duo selection and per-call selector isolation."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json

import pytest

from opencollab.builtin_workflows import _file_selection, _prompts, _selection
from tests.support.duo_test_support import (
    Context,
    decision,
)

g22 = importlib.import_module("opencollab.builtin_workflows.duo")

WORKFLOW = "duo"


class ScriptedContext(Context):
    def __init__(self, *responses):
        super().__init__()
        self.responses = responses

    async def agent(self, prompt, **options):
        self.selector_calls.append((prompt, options))
        response = self.responses[len(self.selector_calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


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
@pytest.mark.parametrize("coverage", ["covered", "not_covered", "unclear"])
def test_judge_issue_recognizes_complete_equal_coverage_as_a_tie(winner, coverage):
    result = coverage_decision(winner=winner, winner_coverage=coverage, loser_coverage=coverage)
    assert _selection._judge_issue(result, {"A": ["src/handler.py"], "B": ["src/handler.py"]}) == "tie"


@pytest.mark.parametrize("field,value", [
    ("winner", ["B"]),
    ("winner", "C"),
    ("requirements_complete", "true"),
    ("requirements", None),
    ("requirements", []),
    ("requirement", ""),
    ("requirement", 1),
    ("a_coverage", ["covered"]),
    ("b_coverage", "unknown"),
    ("a_evidence", "src/handler.py"),
    ("b_evidence", [1]),
])
async def test_malformed_decisions_remain_rejected_and_allow_one_corrected_recheck(tmp_path, field, value):
    result = copy.deepcopy(decision())
    target = result if field in {"winner", "requirements_complete", "requirements"} else result["requirements"][0]
    target[field] = value
    paths = {"A": ["src/handler.py"], "B": ["src/handler.py"]}
    assert _selection._judge_issue(result, paths) not in {None, "tie"}
    assert _selection._validated_judge_winner(result, paths) is None
    corrected = decision()
    ctx = ScriptedContext(result, corrected)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["judge_result"] == corrected
    assert outcome["selection_reason"] == "contract-adjudicated-after-recheck"
    assert len(ctx.selector_calls) == 2


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
    assert len(ctx.selector_calls) == 1


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
    expected_winner = winner
    expected_reason = (
        "contract-adjudicated" if unsupported_gap == "unclear"
        else "contract-b-regression-default-a" if winner == "A"
        else "contract-evidence-insufficient-default-b"
    )
    assert outcome["winner"] == outcome["adopted"] == expected_winner
    assert outcome["selection_reason"] == expected_reason
    assert len(ctx.selector_calls) == (1 if unsupported_gap == "unclear" else 2)


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("coverage", ["unclear", "covered", "not_covered"])
async def test_duo_defaults_to_b_without_coverage_advantage_and_skips_recheck(tmp_path, winner, coverage):
    result = coverage_decision(winner=winner, winner_coverage=coverage, loser_coverage=coverage)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 1


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("evidence", ["", "docs/overview.md describes the return value"])
async def test_duo_rejects_unsupported_coverage_advantage(tmp_path, winner, evidence):
    result = coverage_decision(winner=winner, winner_evidence=evidence)
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == result
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 2


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
    assert outcome["selection_reason"] == "contract-b-regression-default-a"
    assert len(ctx.selector_calls) == 2


@pytest.mark.asyncio
async def test_g22_preserves_original_evidence_rejection_and_defaults_to_b(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    result = decision("The candidate implements the requirement")
    ctx = Context(result=result)
    outcome = await g22.duo(
        ctx, {"goal": "Preserve the public return value"},
    )
    assert outcome["judge_result"] == result
    assert outcome["prompt_revision"] == 7
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 2


@pytest.mark.asyncio
async def test_g22_identical_candidates_keep_mechanical_selection():
    ctx = Context(identical=True)
    outcome = await g22.duo(ctx, {"goal": "Repair public behavior"})
    assert outcome["prompt_revision"] == 7
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "identical-diff"
    assert outcome["judge_used"] is False and not ctx.selector_calls


async def test_parallel_duo_calls_keep_task_and_evidence_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    monkeypatch.setattr(_file_selection, "_INLINE_EVIDENCE_MAX_BYTES", 0)
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
    assert _prompts._PROMPT_REVISION == 7


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
    assert "requirements_complete describes the explicit requirement inventory" in prompt
    assert "requirements either or both candidates leave not_covered or unclear" in prompt
    description = options["schema"]["properties"]["requirements_complete"]["description"]
    assert "inventory completeness, not candidate correctness" in description
    assert "not_covered or unclear" in description


async def test_incomplete_inventory_still_rejects_a_b_recommendation(tmp_path):
    result = decision()
    result["requirements_complete"] = False
    ctx = Context(result=result)
    outcome = await g22.duo(ctx, {"goal": "Cover all requested behavior", "candidate_evidence_dir": str(tmp_path)})
    assert outcome["judge_result"] == result
    assert outcome["winner"] == "B"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 2


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
    assert outcome["selection_reason"] == "contract-b-regression-default-a"
    assert len(ctx.selector_calls) == 2


async def test_sqlite_recommendation_inconsistent_with_coverage_is_corrected_once(tmp_path):
    first = coverage_decision(winner="A", loser_coverage="unclear")
    first["winner"] = "B"
    requirement = "Preserve SQLite inserts that omit a generated primary key"
    first["requirements"][0]["requirement"] = requirement
    first["rationale"] = "B has the preferred SQLite implementation"
    corrected = coverage_decision(winner="B", loser_coverage="unclear")
    corrected["requirements"][0]["requirement"] = requirement
    ctx = ScriptedContext(first, corrected)
    outcome = await g22.duo(ctx, {
        "goal": requirement, "candidate_evidence_dir": str(tmp_path),
    })
    assert _selection._validated_judge_winner(first, {"A": ["src/handler.py"], "B": ["src/handler.py"]}) is None
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["judge_result"] == corrected
    assert outcome["selection_reason"] == "contract-adjudicated-after-recheck"
    assert len(ctx.selector_calls) == 2
    initial_prompt, initial_options = ctx.selector_calls[0]
    recheck_prompt, recheck_options = ctx.selector_calls[1]
    assert first["rationale"] in recheck_prompt
    assert requirement in initial_prompt and requirement in recheck_prompt
    assert "recheck" in recheck_prompt.lower()
    assert recheck_options["schema"] == initial_options["schema"]
    assert recheck_options["tools"] == initial_options["tools"] == []
    initial_evidence, _ = json.JSONDecoder().raw_decode(initial_prompt.split("\nCandidate evidence\n", 1)[1])
    recheck_evidence, _ = json.JSONDecoder().raw_decode(recheck_prompt.split("\nCandidate evidence\n", 1)[1])
    assert recheck_evidence["inline_comparison"] == initial_evidence["inline_comparison"]
    for label, value in [("A", "a"), ("B", "b")]:
        inline = initial_evidence["inline_comparison"][label]
        assert f"-old\n+{value}\n" in inline["diff"]
        assert inline["public_test_records"] == []
        assert inline["candidate_report"] == "Public repair completed"
        assert inline["report_is_model_supplied"] is True


async def test_two_different_invalid_decisions_stop_after_one_recheck(tmp_path):
    first = decision()
    first["requirements_complete"] = False
    second = coverage_decision(winner="A", loser_coverage="unclear")
    second["winner"] = "B"
    ctx = ScriptedContext(first, second)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["judge_result"] == second
    assert outcome["winner"] == outcome["adopted"] == "B"
    assert outcome["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 2


@pytest.mark.parametrize("b_coverage,expected,reason", [
    ("not_covered", "A", "contract-b-regression-default-a"),
    ("unclear", "B", "contract-evidence-insufficient-default-b"),
])
async def test_recheck_provider_failure_preserves_recorded_b_regression(tmp_path, b_coverage, expected, reason):
    first = coverage_decision(winner="A", loser_coverage=b_coverage)
    first["winner"] = "B"
    ctx = ScriptedContext(first, RuntimeError("provider unavailable"))
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["winner"] == outcome["adopted"] == expected
    assert outcome["selection_reason"] == reason
    assert len(ctx.selector_calls) == 2


@pytest.mark.parametrize("winner", ["A", "B"])
async def test_missing_changed_path_can_be_corrected_once_and_select_either_candidate(tmp_path, winner):
    first = coverage_decision(winner=winner, winner_evidence="")
    corrected = coverage_decision(winner=winner)
    ctx = ScriptedContext(first, corrected)
    outcome = await g22.duo(ctx, {
        "goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path),
    })
    assert outcome["winner"] == outcome["adopted"] == winner
    assert outcome["judge_result"] == corrected
    assert outcome["selection_reason"] == "contract-adjudicated-after-recheck"
    assert len(ctx.selector_calls) == 2


@pytest.mark.parametrize("first,second", [(None, None), (decision(), None), (None, decision())])
def test_fallback_defaults_to_b_without_an_explicit_b_regression(first, second):
    assert _selection._fallback_winner(first, second) == "B"


@pytest.mark.parametrize("position", [0, 1])
def test_fallback_protects_a_when_either_response_records_an_explicit_b_regression(position):
    regression = coverage_decision(winner="A", loser_coverage="not_covered")
    regression["requirements_complete"] = False
    responses = [decision(), decision()]
    responses[position] = regression
    assert _selection._fallback_winner(*responses) == "A"


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
