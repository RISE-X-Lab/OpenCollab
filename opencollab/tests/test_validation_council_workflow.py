"""Tests for the validation-council workflow orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


@pytest.fixture(scope="module")
def validation_council_solve():
    registry = discover_workflows(str(_WF_DIR))
    return registry.get("validation-council-solve").fn


class _FakeBudget:
    total = None

    def spent(self) -> int:
        return 123


class ScriptedCtx:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False, **kwargs):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label, "tools": tools, **kwargs}
        )
        return self._replies.pop(0)

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)


LOCALIZATION = {
    "summary": "empty widget crashes",
    "root_cause_hypothesis": "parse misses empty input",
    "files": ["widget.py"],
    "public_api": ["widget.parse"],
    "uncertainties": [],
    "definition_of_done": "empty input returns an empty widget",
}

CONTRACTS = {
    "contracts": [
        {
            "id": "C1",
            "statement": "empty input is accepted",
            "scope": "widget.parse",
            "behavior_kind": "desired",
            "evidence": [
                {
                    "source_type": "issue",
                    "file_or_section": "problem statement",
                    "summary": "user reports empty input crash",
                }
            ],
            "confidence": "medium",
            "testability": "direct function call",
        }
    ]
}

CARTOGRAPHY = {
    "framework": "pytest",
    "runner_commands": ["pytest tests/test_widget.py"],
    "test_files": ["tests/test_widget.py"],
    "fixtures": [],
    "assertion_style": "plain assert",
    "temporary_test_guidance": "use python -c probes",
}

CANDIDATES = {
    "tests": [
        {
            "id": "T1",
            "contract_ids": ["C1"],
            "type": "repro",
            "oracle_type": "return value",
            "setup": "call parse('')",
            "assertion": "returns empty widget",
            "expected_on_base": "fail",
            "expected_on_patch": "pass",
            "why_distinguishes_wrong_patch": "catches empty-input crash",
            "evidence_refs": ["C1"],
            "runner_command": "python -c \"import widget; widget.parse('')\"",
            "risk_of_false_positive": "low",
        }
    ],
    "abstained": False,
    "rationale": "direct repro",
}

JUDGE = {
    "accepted": [
        {"id": "T1", "priority": 1, "classification": "repro", "reason": "contract backed"}
    ],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}

TRIAGE = {
    "classifications": [
        {"test_id": "T1", "status": "base_fail_repro", "evidence": "raises ValueError"}
    ],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}

RISKS = {
    "risks": [
        {
            "id": "R1",
            "changed_area": "widget.parse",
            "risk": "None handling regresses",
            "contract_ids": ["C1"],
            "suggested_probe": "parse(None)",
            "priority": 1,
        }
    ],
    "summary": "small parser risk",
}

PASS = {
    "verdict": "PASS",
    "findings": "validated",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}
FAIL = {
    "verdict": "FAIL",
    "findings": "edge case still fails",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}
BLOCKED = {
    "verdict": "BLOCKED",
    "findings": "dependency unavailable",
    "allowed_patch_paths": [],
    "disallowed_patch_paths": [],
}


def _base_replies(final_verdict: dict[str, str] = PASS) -> list[Any]:
    return [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        final_verdict,
    ]


async def test_happy_path_passes_first_round(validation_council_solve):
    ctx = ScriptedCtx(_base_replies())

    result = await validation_council_solve(
        ctx,
        {
            "description": "fix empty widget",
            "fail_to_pass": ["tests/hidden.py::test_secret"],
            "injected_test_paths": ["tests/hidden.py"],
        },
    )

    assert result["status"] == "done"
    assert result["rounds"] == 1
    assert result["contracts"] == 1
    assert result["pre_validation_accepted"] == 1
    assert result["allowed_patch_paths"] == ["widget.py"]
    assert result["disallowed_patch_paths"] == []
    assert result["tokens_spent"] == 123
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
        "patch-validator:r1",
        "diff-risk-auditor:r1",
        "post-validation-factory:r1",
        "post-r1-validation-judge",
        "post-validation-triage:r1",
        "final-verifier:r1",
    ]
    budgets = {call["label"]: call.get("budget") for call in ctx.agent_calls}
    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert budgets["analyst-localizer"] == 220_000
    assert budgets["contract-miner"] == 180_000
    assert budgets["test-cartographer"] == 180_000
    assert budgets["pre-validation-factory"] == 160_000
    assert budgets["pre-validation-judge"] == 100_000
    assert budgets["baseline-triage"] == 180_000
    assert budgets["coder:r1"] is None
    assert budgets["patch-validator:r1"] == 220_000
    assert budgets["diff-risk-auditor:r1"] == 60_000
    assert budgets["post-validation-factory:r1"] == 160_000
    assert budgets["post-r1-validation-judge"] == 100_000
    assert budgets["post-validation-triage:r1"] == 180_000
    assert budgets["final-verifier:r1"] == 220_000
    assert timeouts["coder:r1"] == 1800
    for call in ctx.agent_calls:
        if call["label"] != "coder:r1":
            assert call.get("timeout") == 300
    assert ctx.phases == [
        "localize",
        "evidence",
        "pre-validate",
        "solve:r1",
        "diff-risk:r1",
        "final-verify:r1",
    ]
    all_prompts = "\n".join(call["prompt"] for call in ctx.agent_calls)
    assert "tests/hidden.py::test_secret" not in all_prompts
    assert "If your current tools cannot create or run a probe" in all_prompts
    assert "do not search for write tools" in all_prompts


async def test_failed_final_verifier_retries_with_feedback(validation_council_solve):
    ctx = ScriptedCtx(_base_replies(FAIL) + _base_replies(PASS)[6:])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert "edge case still fails" in ctx.agent_calls[13]["prompt"]
    budgets = {call["label"]: call.get("budget") for call in ctx.agent_calls}
    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert budgets["coder:r2"] is None
    assert budgets["patch-validator:r2"] == 220_000
    assert budgets["diff-risk-auditor:r2"] == 60_000
    assert budgets["post-validation-factory:r2"] == 160_000
    assert budgets["post-r2-validation-judge"] == 100_000
    assert budgets["post-validation-triage:r2"] == 180_000
    assert budgets["final-verifier:r2"] == 220_000
    assert timeouts["coder:r2"] == 1800
    assert timeouts["patch-validator:r2"] == 300
    assert timeouts["diff-risk-auditor:r2"] == 300
    assert timeouts["post-validation-factory:r2"] == 300
    assert timeouts["post-r2-validation-judge"] == 300
    assert timeouts["post-validation-triage:r2"] == 300
    assert timeouts["final-verifier:r2"] == 300
    assert any("attempt 1 failed" in message for message in ctx.logs)


async def test_blocked_patch_validator_short_circuits_retry(validation_council_solve):
    replies = [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        BLOCKED,
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "blocked"
    assert result["rounds"] == 1
    assert result["blocker"] == "dependency unavailable"
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
        "patch-validator:r1",
    ]
    assert any("attempt 1 blocked" in message for message in ctx.logs)


async def test_blocked_final_verifier_short_circuits_retry(validation_council_solve):
    replies = [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        BLOCKED,
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "blocked"
    assert result["rounds"] == 1
    assert result["blocker"] == "dependency unavailable"
    assert [call["label"] for call in ctx.agent_calls][-1] == "final-verifier:r1"
    assert not any(call["label"] == "coder:r2" for call in ctx.agent_calls)


async def test_missing_goal_is_an_error_before_any_agent(validation_council_solve):
    ctx = ScriptedCtx([])

    result = await validation_council_solve(ctx, {})

    assert result["status"] == "error"
    assert ctx.agent_calls == []


def test_discovery_registers_validation_council_workflow():
    registry = discover_workflows(str(_WF_DIR))
    names = [s.name for s in registry.list_specs()]
    assert "validation-council-solve" in names
