"""Tests for the SWE Committee V2 graph isomorphism."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"


@pytest.fixture(scope="module")
def swe_committee_v2():
    registry = discover_workflows(str(_WF_DIR))
    return registry.get("swe-committee-v2").fn


class _FakeBudget:
    total = None

    def spent(self) -> int:
        return 321


class ScriptedCtx:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label, "tools": tools}
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
    "scope_files": ["widget.py"],
    "public_api": ["widget.parse"],
    "uncertainties": [],
    "definition_of_done": "empty input returns an empty widget",
}

CONTRACTS = {
    "contracts": [
        {
            "id": "C1",
            "statement": "empty input is accepted",
            "behavior_kind": "desired",
            "evidence": [
                {
                    "source_type": "issue",
                    "file_or_section": "problem statement",
                    "summary": "user reports empty input crash",
                }
            ],
            "confidence": "high",
            "testability": "direct function call",
        }
    ]
}

CARTOGRAPHY = {
    "framework": "pytest",
    "runner_commands": ["pytest tests/test_widget.py"],
    "test_files": ["tests/test_widget.py"],
    "fixtures": [],
    "temporary_test_guidance": "use python -c probes",
}

INVENTORY = {
    "observable_fields": ["return value", "exception type"],
    "unaffected_fields": ["ordering"],
    "risky_fields": ["None handling"],
    "notes": "small parser surface",
}

TRIBUNAL = {
    "contracts": [
        {
            "id": "C1",
            "statement": "empty input is accepted",
            "evidence_tier": "strong",
            "evidence": ["issue text"],
            "contract_type": "desired",
        }
    ],
    "strong_contract_ids": ["C1"],
    "weak_contract_ids": [],
    "hypothesis_contract_ids": [],
    "forbidden_fields": ["None handling"],
    "rationale": "direct issue evidence",
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
    "verdict": "PASS",
    "accepted": [
        {"id": "T1", "priority": 1, "classification": "repro", "reason": "contract backed"}
    ],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}

TRIAGE = {
    "classifications": [
        {"test_id": "T1", "status": "base-fail", "evidence": "raises ValueError"}
    ],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}

ETV = {
    "status": "PASS",
    "findings": "approved checks pass",
    "checks": [{"name": "T1", "status": "pass", "evidence": "returns empty"}],
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
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
    "evidence": ["git diff"],
}

POST_PASS = {
    "verdict": "PASS",
    "accepted": [],
    "rejected": [],
    "diagnostic": ["no strong post probes"],
    "triage": [],
    "validation_brief": "no evidence-backed failure",
    "retry_feedback": "",
}

POST_FAIL = {
    "verdict": "FAIL_WITH_EVIDENCE",
    "accepted": [
        {"id": "P1", "priority": 1, "classification": "edge", "reason": "strong contract"}
    ],
    "rejected": [],
    "diagnostic": [],
    "triage": [{"test_id": "P1", "status": "patch-fail", "evidence": "still raises"}],
    "validation_brief": "post probe fails",
    "retry_feedback": "handle the empty string branch",
}

SKEPTIC_PASS = {
    "verdict": "PASS",
    "findings": "contracts support the patch",
    "required_evidence": [],
    "next_action": "",
}

SKEPTIC_BLOCK = {
    "verdict": "CONTRACT_EVIDENCE_BLOCKER",
    "findings": "None handling has no contract evidence",
    "required_evidence": ["direct evidence for None"],
    "next_action": "downgrade None behavior before retry",
}

FINAL_PASS = {
    "verdict": "PASS",
    "findings": "ready",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
    "retry_feedback": "",
}

FINAL_BLOCK = {
    "verdict": "CONCRETE_BLOCKER",
    "findings": "temporary test artifact is still present",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": ["tests/test_probe.py"],
    "retry_feedback": "remove temporary test artifact",
}


def _front_half() -> list[Any]:
    return [LOCALIZATION, CONTRACTS, CARTOGRAPHY, INVENTORY, TRIBUNAL, CANDIDATES, JUDGE, TRIAGE]


def _attempt_tail(*, post=POST_PASS, skeptic=SKEPTIC_PASS, final=FINAL_PASS) -> list[Any]:
    replies: list[Any] = [
        "changed widget.py",
        ETV,
        RISKS,
        RISKS,
        RISKS,
        CANDIDATES,
        post,
    ]
    if post["verdict"] == "FAIL_WITH_EVIDENCE":
        return replies
    replies.append(skeptic)
    if skeptic["verdict"] == "CONTRACT_EVIDENCE_BLOCKER":
        return replies
    replies.append(final)
    return replies


async def test_happy_path_matches_graph_nodes(swe_committee_v2):
    ctx = ScriptedCtx(_front_half() + _attempt_tail())

    result = await swe_committee_v2(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 1
    assert result["attempts"][0]["edge"] == "FV->OUT"
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "observable-contract-inventory",
        "contract-tribunal",
        "pre-patch-validation-factory",
        "pre-validation-judge",
        "baseline-executor-triage",
        "coder:r1",
        "existing-tests-approved-validation:r1",
        "branch-boundary-attack:r1",
        "regression-scan:r1",
        "observable-diff-review:r1",
        "post-validation-factory:r1",
        "post-validation-judge-triage:r1",
        "final-skeptic:r1",
        "final-verifier:r1",
    ]


async def test_vj2_fail_with_evidence_routes_to_coder_minimal_retry(swe_committee_v2):
    ctx = ScriptedCtx(_front_half() + _attempt_tail(post=POST_FAIL) + _attempt_tail())

    result = await swe_committee_v2(ctx, {"goal": "fix empty widget"})

    labels = [call["label"] for call in ctx.agent_calls]
    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert result["attempts"][0]["edge"] == "VJ2->CR"
    assert "coder-minimal-retry:r2" in labels
    assert labels.count("contract-tribunal") == 1
    assert "final-skeptic:r1" not in labels


async def test_final_skeptic_blocker_routes_to_contract_tribunal_then_retry(swe_committee_v2):
    replies = (
        _front_half()
        + _attempt_tail(skeptic=SKEPTIC_BLOCK)
        + [TRIBUNAL, CANDIDATES, JUDGE, TRIAGE]
        + _attempt_tail()
    )
    ctx = ScriptedCtx(replies)

    result = await swe_committee_v2(ctx, {"goal": "fix empty widget"})

    labels = [call["label"] for call in ctx.agent_calls]
    assert result["status"] == "done"
    assert result["attempts"][0]["edge"] == "FS->CA"
    assert labels.count("contract-tribunal") == 2
    assert "pre-patch-validation-factory:rearbitrate-r1" in labels
    assert "pre-rearbitrate-r1-validation-judge" in labels
    assert "baseline-executor-triage:rearbitrate-r1" in labels
    assert "final-verifier:r1" not in labels
    assert "coder-minimal-retry:r2" in labels


async def test_final_verifier_concrete_blocker_routes_to_retry_without_tribunal(swe_committee_v2):
    ctx = ScriptedCtx(_front_half() + _attempt_tail(final=FINAL_BLOCK) + _attempt_tail())

    result = await swe_committee_v2(ctx, {"goal": "fix empty widget"})

    labels = [call["label"] for call in ctx.agent_calls]
    assert result["status"] == "done"
    assert result["attempts"][0]["edge"] == "FV->CR"
    assert "coder-minimal-retry:r2" in labels
    assert labels.count("contract-tribunal") == 1
    assert "final-verifier:r1" in labels
    assert "final-verifier:r2" in labels
