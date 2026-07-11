"""swe-committee-v2 - committee-style SWE workflow with explicit stage boundaries.

This workflow is implemented from the requested committee graph:
Analyst/Localizer -> Evidence Stage -> Contract Tribunal -> Pre-patch
validation -> Baseline triage -> Coder -> Existing Tests + Approved Validation
-> Patch Attack Stage -> Post-patch validation -> Final skeptic -> Final
verifier, with bounded Coder Minimal Retry rounds.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.workflow_registry import workflow

MAX_PRE_TESTS = 5
MAX_POST_TESTS = 4
MAX_CODER_ROUNDS = 3


EMPTY_POST_CANDIDATES = {
    "tests": [],
    "abstained": True,
    "rationale": "Post-patch validation skipped.",
}
EMPTY_POST_JUDGE = {
    "verdict": "PASS",
    "accepted": [],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "Post-patch validation skipped.",
    "triage": [],
    "retry_feedback": "",
}
EMPTY_DIFF_RISKS = {
    "risks": [],
    "summary": "No diff-risk signal emitted.",
    "evidence": [],
}
EMPTY_SKEPTIC = {
    "verdict": "PASS",
    "findings": "No skeptic signal.",
    "required_evidence": [],
    "next_action": "Retry only on explicit contract or verifier blockers.",
}

EMPTY_FINAL_VERIFIER = {
    "verdict": "CONCRETE_BLOCKER",
    "findings": "Final verifier returned no structured verdict.",
    "allowed_patch_paths": [],
    "disallowed_patch_paths": [],
    "retry_feedback": "Re-check the patch against approved validation and diff boundaries.",
}


SHARED_RULES = """\
Rules:
- Use only issue text, repository source, public tests, and public docs.
- Do not use hidden grader test patches, injected FAIL_TO_PASS IDs, or any hidden suite.
- Prefer file_read/grep for inspection and run_tests for validation checks.
- Use bash only when no dedicated tool fits.
- Never include temporary validation artifacts or test scripts in final patch output.
- Keep temporary validation outside git diff; remove temporary validation files before final verifier pass.
- Minimize edits: prefer smallest behavior-preserving patch.
- Never run git commit.
"""

LOCALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "root_cause_hypothesis",
        "scope_files",
        "public_api",
        "uncertainties",
        "definition_of_done",
    ],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_hypothesis": {"type": "string"},
        "scope_files": {"type": "array", "items": {"type": "string"}},
        "public_api": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "definition_of_done": {"type": "string"},
    },
}

CONTRACT_MINER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["contracts"],
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "statement",
                    "behavior_kind",
                    "evidence",
                    "confidence",
                    "testability",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "behavior_kind": {
                        "type": "string",
                        "enum": ["desired", "current_buggy", "existing_unaffected"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["source_type", "file_or_section", "summary"],
                            "properties": {
                                "source_type": {"type": "string"},
                                "file_or_section": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                        },
                    },
                    "confidence": {"type": "string"},
                    "testability": {"type": "string"},
                },
            },
        }
    },
}

TEST_CARTOGRAPHY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "runner_commands",
        "test_files",
        "fixtures",
        "temporary_test_guidance",
    ],
    "properties": {
        "framework": {"type": "string"},
        "runner_commands": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "fixtures": {"type": "array", "items": {"type": "string"}},
        "temporary_test_guidance": {"type": "string"},
    },
}

OBSERVABLE_INVENTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "observable_fields",
        "unaffected_fields",
        "risky_fields",
        "notes",
    ],
    "properties": {
        "observable_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Potentially observable behavior outputs, exception shapes, return values, ordering.",
        },
        "unaffected_fields": {"type": "array", "items": {"type": "string"}},
        "risky_fields": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

TRIBUNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "contracts",
        "strong_contract_ids",
        "weak_contract_ids",
        "hypothesis_contract_ids",
        "forbidden_fields",
        "rationale",
    ],
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "statement",
                    "evidence_tier",
                    "evidence",
                    "contract_type",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "evidence_tier": {
                        "type": "string",
                        "enum": ["strong", "weak", "speculative", "forbidden"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "contract_type": {
                        "type": "string",
                        "enum": ["desired", "current_buggy", "existing_unaffected"],
                    },
                },
            },
        },
        "strong_contract_ids": {"type": "array", "items": {"type": "string"}},
        "weak_contract_ids": {"type": "array", "items": {"type": "string"}},
        "hypothesis_contract_ids": {"type": "array", "items": {"type": "string"}},
        "forbidden_fields": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
}

BASELINE_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "accepted", "rejected", "diagnostic", "validation_brief"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "accepted": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "priority", "classification", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "priority": {"type": "integer"},
                    "classification": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "rejected": {"type": "array", "items": {"type": "object"}},
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "validation_brief": {"type": "string"},
    },
}

CANDIDATE_TESTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tests", "abstained", "rationale"],
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "contract_ids",
                    "type",
                    "oracle_type",
                    "setup",
                    "assertion",
                    "expected_on_base",
                    "expected_on_patch",
                    "why_distinguishes_wrong_patch",
                    "evidence_refs",
                    "runner_command",
                    "risk_of_false_positive",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "type": {
                        "type": "string",
                        "enum": ["repro", "edge", "regression", "metamorphic", "diagnostic"],
                    },
                    "oracle_type": {"type": "string"},
                    "setup": {"type": "string"},
                    "assertion": {"type": "string"},
                    "expected_on_base": {
                        "type": "string",
                        "enum": ["fail", "pass", "unknown"],
                    },
                    "expected_on_patch": {
                        "type": "string",
                        "enum": ["pass", "unknown"],
                    },
                    "why_distinguishes_wrong_patch": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "runner_command": {"type": "string"},
                    "risk_of_false_positive": {"type": "string"},
                },
            },
        },
        "abstained": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["classifications", "approved_brief", "abstained"],
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "status", "evidence"],
                "properties": {
                    "test_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["base-fail", "base-pass", "patch-pass", "patch-fail", "invalid", "weak", "not_run"],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "approved_brief": {"type": "string"},
        "abstained": {"type": "boolean"},
    },
}

DIFF_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["risks", "summary", "evidence"],
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "changed_area", "risk", "contract_ids", "suggested_probe", "priority"],
                "properties": {
                    "id": {"type": "string"},
                    "changed_area": {"type": "string"},
                    "risk": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "suggested_probe": {"type": "string"},
                    "priority": {"type": "integer"},
                },
            },
        },
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}

ETV_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "findings",
        "checks",
        "allowed_patch_paths",
        "disallowed_patch_paths",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "FAIL", "NOT_RUN"]},
        "findings": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "status", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "allowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "disallowed_patch_paths": {"type": "array", "items": {"type": "string"}},
    },
}

POST_VALIDATION_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "verdict",
        "accepted",
        "rejected",
        "diagnostic",
        "triage",
        "validation_brief",
        "retry_feedback",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL_WITH_EVIDENCE"]},
        "accepted": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "priority", "classification", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "priority": {"type": "integer"},
                    "classification": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "rejected": {"type": "array", "items": {"type": "object"}},
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "triage": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "status", "evidence"],
                "properties": {
                    "test_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["patch-pass", "patch-fail", "invalid", "weak", "not_run"],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "validation_brief": {"type": "string"},
        "retry_feedback": {"type": "string"},
    },
}

SKEPTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "required_evidence", "next_action"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "CONTRACT_EVIDENCE_BLOCKER"]},
        "findings": {"type": "string"},
        "required_evidence": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
    },
}

FINAL_VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "verdict",
        "findings",
        "allowed_patch_paths",
        "disallowed_patch_paths",
        "retry_feedback",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "CONCRETE_BLOCKER"]},
        "findings": {"type": "string"},
        "allowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "disallowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "retry_feedback": {"type": "string"},
    },
}


LOCALIZER_PROMPT = """\
You are Analyst / Localizer.
Read only. Build the root-cause map for one SWE issue: likely files, public API
touchpoints, hypothesis and uncertainties, and a precise definition of done.

{rules}

Goal:
{goal}"""

CONTRACT_MINER_PROMPT = """\
You are the Contract Miner.
Extract minimal behavior contracts from issue text, source, public tests, and docs.
Each contract must be evidence-backed and tagged by behavior kind.

{rules}

Goal:
{goal}

Localization:
{localization}"""

TEST_CARTOGRAPHER_PROMPT = """\
You are the Test Cartographer.
Map how this repository exposes observable behavior: test framework, runner
commands, relevant public test files, and temporary validation scaffolding.

{rules}

Goal:
{goal}

Localization:
{localization}"""

CONTRACT_INVENTORY_PROMPT = """\
You are Observable Contract Inventory.
List concrete externally visible fields and interactions for this issue context:
return values, exceptions, warnings/messages, ordering, defaults, and public side
effects that should be preserved.

{rules}

Goal:
{goal}

Localization:
{localization}"""

CONTRACT_TRIBUNAL_PROMPT = """\
You are Contract Tribunal.
Merge Contract Miner, Test Cartographer, and Observable Contract Inventory into a
single evidence protocol.

- Produce a strong contract set with direct evidence and high confidence.
- Produce a weak set from one-line or indirect evidence.
- Keep a hypothesis set only when clearly unresolved.
- Explicitly list forbidden fields: anything you have insufficient evidence to
  assert as stable.
- Do not keep guessed behavior in strong contracts.

If the previous attempt had blockers, incorporate them and downgrade speculative
items first.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Miner:
{contract_miner}

Test Cartographer:
{test_cartographer}

Observable Inventory:
{contract_inventory}

Previous Arbitration Notes:
{feedback}"""

CANDIDATE_GENERATION_PROMPT = """\
You are the Pre-Patch Validation Factory.
Propose strong validation probes for the most likely root-cause.

Do not emit probes that rely on hidden grader tests or guessed internals.
Link each probe to valid contract ids.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Test Cartography:
{test_cartography}"""

BASELINE_JUDGE_PROMPT = """\
You are Validation Judge / Prioritizer for Pre-Patch stage.
Reject weak proposals quickly. Accept at most {cap} candidates with clear contract
coverage and reproducible evidence.

{rules}

Goal:
{goal}

Contracts (from Tribunal):
{tribunal}

Candidates:
{candidates}"""

BASELINE_TRIAGE_PROMPT = """\
You are Baseline Executor + Triage.
Run accepted probes cheaply. Classify each as base-fail, base-pass, patch-fail,
invalid, weak, or not_run. Report command evidence and classify clearly.

{rules}

Goal:
{goal}

Baseline candidates:
{judge}"""

CODER_PROMPT = """\
You are the Coder.
Implement a minimal source fix consistent with the localization, contracts, and tests.
Do not edit tests unless explicitly required.
Run practical focused checks and keep temporary validation files outside final diff.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Test Cartography:
{cartography}

Pre-patch judge:
{pre_judge}

Baseline triage:
{baseline_triage}

{feedback_block}
"""

ETV_PROMPT = """\
You are Existing Tests + Approved Validation.
Run the existing tests and the approved validation checks that are practical for
this patch. Record observed results and patch path boundaries.

Classify allowed paths (source fixes only) and disallowed paths (temporary files,
tests, caches, logs, notes, artifacts).

{rules}

Goal:
{goal}

Coder report:
{coder_report}

Pre-patch judge:
{pre_judge}

Baseline triage:
{baseline_triage}"""

DIFF_RISK_BRANCH_PROMPT = """\
You are Branch / Boundary Attack.
Use git diff and temporary reasoning to enumerate behavior-risky boundary and branch
changes, and concrete checks that would catch missed corner cases.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Existing Tests + Approved Validation:
{patch_verdict}"""

DIFF_RISK_REGRESSION_PROMPT = """\
You are Regression Impact Scan.
From contract and diff context, identify likely regressions in neighboring behavior.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Existing Tests + Approved Validation:
{patch_verdict}"""

DIFF_RISK_OBSERVABLE_PROMPT = """\
You are Observable Diff Review.
Read the current diff and identify changed observable fields that lack direct contract
support from pre-existing evidence.

{rules}

Goal:
{goal}

Contract Inventory:
{inventory}

Existing Tests + Approved Validation:
{patch_verdict}"""

POST_VALIDATION_FACTORY_PROMPT = """\
You are Post-Patch Validation Factory.
Propose targeted post-patch probes for strongest remaining risks.

{rules}

Goal:
{goal}

Contract Tribunal:
{tribunal}

Diff risks:
{diff_risks}"""

POST_JUDGE_TRIAGE_PROMPT = """\
You are Post Validation Judge + Triage.
Reject probes that encode guesses as expected behavior. Accept at most {cap}
evidence-backed probes, run the accepted checks when practical, and triage the
results.

Return FAIL_WITH_EVIDENCE only when an accepted evidence-backed check clearly
fails on the current patch. Return PASS for invalid, weak, diagnostic-only, or
not-run checks.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Candidates:
{candidates}

Existing Tests + Approved Validation:
{etv_report}"""

FINAL_SKEPTIC_PROMPT = """\
You are Final Skeptic.
Return CONTRACT_EVIDENCE_BLOCKER only when the patch depends on an unsupported
contract claim. Examples: claims over weak/hypothetical contracts, changes to
fields not authorized by the tribunal, or un-audited side effects without
evidence.

Return PASS for concrete patch problems that can be sent directly to the coder.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Existing Tests + Approved Validation:
{etv_report}

Post validation decision:
{post_decision}

Diff risks:
{risks}"""

FINAL_VERIFIER_PROMPT = """\
You are Final Verifier.
Confirm issue fix, minimal source change, and clean diff boundaries.
Return PASS only when the patch is ready as the final model_patch.
Return CONCRETE_BLOCKER for a concrete fix/test/diff issue that the Coder Minimal
Retry can act on without re-opening contract arbitration.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Existing Tests + Approved Validation:
{etv_report}

Diff risks:
{risks}

Post validation:
{post_decision}

Skeptic:
{skeptic}"""


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _dict_or(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else fallback


def _trim(judge: dict[str, Any], cap: int) -> dict[str, Any]:
    accepted = judge.get("accepted")
    if isinstance(accepted, list):
        judge = {**judge, "accepted": accepted[:cap]}
    return judge


def _is_pass(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "PASS"


def _post_failed_with_evidence(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "FAIL_WITH_EVIDENCE"


def _contract_evidence_blocker(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "CONTRACT_EVIDENCE_BLOCKER"


def _concrete_blocker(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "CONCRETE_BLOCKER"


def _accepted_count(judge: Any) -> int:
    return len(judge.get("accepted")) if isinstance(judge, dict) and isinstance(judge.get("accepted"), list) else 0


def _feedback(*reports: Any) -> str:
    parts: list[str] = []
    for report in reports:
        if isinstance(report, dict):
            for key in ("findings", "approved_brief", "summary", "rationale", "next_action"):
                value = report.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        elif isinstance(report, str) and report.strip():
            parts.append(report.strip())
    return "\n\n".join(parts) or "No structured feedback."


def _judge_default() -> dict[str, Any]:
    return {
        "verdict": "FAIL",
        "accepted": [],
        "rejected": [],
        "diagnostic": [],
        "validation_brief": "No structured judge output was returned.",
    }


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    tribunal: dict[str, Any],
    candidates: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await ctx.agent(
        BASELINE_JUDGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            tribunal=_dump(tribunal),
            candidates=_dump(candidates),
            cap=cap,
        ),
        schema=BASELINE_JUDGE_SCHEMA,
        label=f"{stage}-validation-judge",
        tools=_read_tools(),
    )
    return _trim(_dict_or(judge, _judge_default()), cap)


def _merge_risks(*risks: Any) -> dict[str, Any]:
    merged: list[dict[str, Any]] = []
    summary: list[str] = []
    evidence: list[str] = []
    seen: set[str] = set()

    for risk in risks:
        block = _dict_or(risk, EMPTY_DIFF_RISKS)
        if isinstance(block, dict):
            summary.append(block.get("summary", "").strip())
            evidence.extend(entry for entry in block.get("evidence", []) if isinstance(entry, str))
            for item in block.get("risks", []):
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id", ""))
                if rid in seen:
                    continue
                seen.add(rid)
                merged.append(item)

    return {
        "risks": merged,
        "summary": "\n\n".join(part for part in summary if part),
        "evidence": evidence,
    }


async def _run_contract_tribunal(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    contracts: dict[str, Any],
    cartography: dict[str, Any],
    inventory: dict[str, Any],
    feedback: str,
) -> dict[str, Any]:
    result = await ctx.agent(
        CONTRACT_TRIBUNAL_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contract_miner=_dump(contracts),
            test_cartographer=_dump(cartography),
            contract_inventory=_dump(inventory),
            feedback=feedback,
        ),
        schema=TRIBUNAL_SCHEMA,
        label="contract-tribunal",
        tools=_read_tools(),
    )
    tribunal = _dict_or(
        result,
        {
            "contracts": [],
            "strong_contract_ids": [],
            "weak_contract_ids": [],
            "hypothesis_contract_ids": [],
            "forbidden_fields": [],
            "rationale": "No structured tribunal output.",
        },
    )
    if not isinstance(tribunal.get("contracts"), list):
        tribunal["contracts"] = []
    if not isinstance(tribunal.get("strong_contract_ids"), list):
        tribunal["strong_contract_ids"] = []
    if not isinstance(tribunal.get("weak_contract_ids"), list):
        tribunal["weak_contract_ids"] = []
    if not isinstance(tribunal.get("hypothesis_contract_ids"), list):
        tribunal["hypothesis_contract_ids"] = []
    if not isinstance(tribunal.get("forbidden_fields"), list):
        tribunal["forbidden_fields"] = []
    return tribunal


async def _run_pre_patch_validation(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    tribunal: dict[str, Any],
    cartography: dict[str, Any],
    label_suffix: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    suffix = f":{label_suffix}" if label_suffix else ""
    judge_stage = f"pre-{label_suffix}" if label_suffix else "pre"
    pre_candidates = await ctx.agent(
        CANDIDATE_GENERATION_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            test_cartography=_dump(cartography),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"pre-patch-validation-factory{suffix}",
        tools=_read_tools(),
    )
    pre_candidates = _dict_or(
        pre_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured pre-patch candidates."},
    )

    pre_judge = await _judge_candidates(
        ctx,
        goal=goal,
        tribunal=tribunal,
        candidates=pre_candidates,
        stage=judge_stage,
        cap=MAX_PRE_TESTS,
    )
    if pre_judge.get("verdict") == "BLOCKED":
        pre_judge["verdict"] = "FAIL"

    baseline_triage = await ctx.agent(
        BASELINE_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(pre_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label=f"baseline-executor-triage{suffix}",
        tools=_tester_tools(),
    )
    baseline_triage = _dict_or(
        baseline_triage,
        {"classifications": [], "approved_brief": "No structured baseline triage.", "abstained": True},
    )
    return pre_candidates, pre_judge, baseline_triage


async def _run_attempt(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    cartography: dict[str, Any],
    tribunal: dict[str, Any],
    pre_judge: dict[str, Any],
    baseline_triage: dict[str, Any],
    inventory: dict[str, Any],
    attempt: int,
    feedback: str,
) -> dict[str, Any]:
    feedback_block = FEEDBACK_BLOCK.format(feedback=feedback) if feedback else ""
    coder_label = "coder:r1" if attempt == 1 else f"coder-minimal-retry:r{attempt}"
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            cartography=_dump(cartography),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            feedback_block=feedback_block,
        ),
        label=coder_label,
        tools=_coder_tools(),
    )

    await ctx.phase(f"existing-tests-approved-validation:r{attempt}")
    etv_report = await ctx.agent(
        ETV_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            coder_report=coder_report or "(coder returned no report)",
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
        ),
        schema=ETV_SCHEMA,
        label=f"existing-tests-approved-validation:r{attempt}",
        tools=_tester_tools(),
    )
    etv_report = _dict_or(
        etv_report,
        {
            "status": "NOT_RUN",
            "findings": "Existing tests and approved validation returned no structured report.",
            "checks": [],
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )

    await ctx.phase(f"diff-risk:r{attempt}")
    def branch_risk_task():
        return ctx.agent(
            DIFF_RISK_BRANCH_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                tribunal=_dump(tribunal),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"branch-boundary-attack:r{attempt}",
            tools=_tester_tools(),
        )

    def regression_risk_task():
        return ctx.agent(
            DIFF_RISK_REGRESSION_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                tribunal=_dump(tribunal),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"regression-scan:r{attempt}",
            tools=_tester_tools(),
        )

    def observable_risk_task():
        return ctx.agent(
            DIFF_RISK_OBSERVABLE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                inventory=_dump(inventory),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"observable-diff-review:r{attempt}",
            tools=_tester_tools(),
        )

    branch_risk, regression_risk, observable_risk = await ctx.parallel([
        branch_risk_task,
        regression_risk_task,
        observable_risk_task,
    ])
    diff_risks = _merge_risks(branch_risk, regression_risk, observable_risk)

    await ctx.phase(f"post-validate:r{attempt}")
    post_candidates = await ctx.agent(
        POST_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            tribunal=_dump(tribunal),
            diff_risks=_dump(diff_risks),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"post-validation-factory:r{attempt}",
        tools=_read_tools(),
    )
    post_candidates = _dict_or(
        post_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured post-patch candidates."},
    )

    await ctx.phase(f"post-validation-judge-triage:r{attempt}")
    post_decision = _trim(
        _dict_or(
            await ctx.agent(
                POST_JUDGE_TRIAGE_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    tribunal=_dump(tribunal),
                    candidates=_dump(post_candidates),
                    etv_report=_dump(etv_report),
                    cap=MAX_POST_TESTS,
                ),
                schema=POST_VALIDATION_DECISION_SCHEMA,
                label=f"post-validation-judge-triage:r{attempt}",
                tools=_tester_tools(),
            ),
            EMPTY_POST_JUDGE,
        ),
        MAX_POST_TESTS,
    )
    if _post_failed_with_evidence(post_decision):
        return {
            "attempt": attempt,
            "status": "coder_minimal_retry",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": EMPTY_SKEPTIC,
            "final_verdict": {
                "verdict": "CONCRETE_BLOCKER",
                "findings": post_decision.get("retry_feedback", "Post validation found an evidence-backed failure."),
                "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
                "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
                "retry_feedback": post_decision.get("retry_feedback", ""),
            },
            "feedback": _feedback(post_decision, etv_report),
            "edge": "VJ2->CR",
            "retry": True,
        }

    await ctx.phase(f"skeptic:r{attempt}")
    skeptic = await ctx.agent(
        FINAL_SKEPTIC_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            etv_report=_dump(etv_report),
            post_decision=_dump(post_decision),
            risks=_dump(diff_risks),
        ),
        schema=SKEPTIC_SCHEMA,
        label=f"final-skeptic:r{attempt}",
        tools=_read_tools(),
    )
    skeptic = _dict_or(
        skeptic,
        {
            "verdict": "PASS",
            "findings": "Final skeptic returned no structured judgement.",
            "required_evidence": [],
            "next_action": "Re-run one minimal retry if verifier still fails.",
        },
    )
    if _contract_evidence_blocker(skeptic):
        return {
            "attempt": attempt,
            "status": "contract_rearbitrate",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": skeptic,
            "final_verdict": {
                "verdict": "CONCRETE_BLOCKER",
                "findings": skeptic.get("findings", "Contract evidence blocker."),
                "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
                "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
                "retry_feedback": skeptic.get("next_action", ""),
            },
            "feedback": _feedback(skeptic, post_decision, etv_report),
            "edge": "FS->CA",
            "retry": True,
        }

    await ctx.phase(f"final-verify:r{attempt}")
    final_verdict = await ctx.agent(
        FINAL_VERIFIER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            etv_report=_dump(etv_report),
            post_decision=_dump(post_decision),
            risks=_dump(diff_risks),
            skeptic=_dump(skeptic),
        ),
        schema=FINAL_VERIFIER_SCHEMA,
        label=f"final-verifier:r{attempt}",
        tools=_tester_tools(),
    )
    final_verdict = _dict_or(
        final_verdict,
        {
            **EMPTY_FINAL_VERIFIER,
            "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
            "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
        },
    )

    if _concrete_blocker(final_verdict):
        return {
            "attempt": attempt,
            "status": "coder_minimal_retry",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": skeptic,
            "final_verdict": final_verdict,
            "edge": "FV->CR",
            "feedback": _feedback(skeptic, final_verdict),
            "retry": True,
        }

    return {
        "attempt": attempt,
        "status": "done",
        "coder_report": coder_report or "",
        "etv_report": etv_report,
        "patch_verdict": etv_report,
        "diff_risks": diff_risks,
        "post_candidates": post_candidates,
        "post_judge": post_decision,
        "post_triage": post_decision,
        "skeptic": skeptic,
        "final_verdict": final_verdict,
        "edge": "FV->OUT",
        "feedback": _feedback(skeptic, final_verdict),
        "retry": False,
    }


FEEDBACK_BLOCK = """
Previous attempt findings:
{feedback}"""


@workflow(
    name="swe-committee-v2",
    description="SWE Committee V2 workflow (Analyst, CM, TC, CI, Tribunal, dual judges, risk audit, skeptical gate).",
    phases=[
        "localize",
        "evidence",
        "contract-tribunal",
        "pre-validate",
        "solve",
        "diff-risk",
        "post-validate",
        "skeptic",
        "final-verify",
    ],
)
async def swe_committee_v2(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("localize")
    localization = await ctx.agent(
        LOCALIZER_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=LOCALIZATION_SCHEMA,
        label="analyst-localizer",
        tools=_read_tools(),
    )
    localization = _dict_or(
        localization,
        {
            "summary": "No structured localization was produced.",
            "root_cause_hypothesis": "",
            "scope_files": [],
            "public_api": [],
            "uncertainties": ["No structured evidence from localization."],
            "definition_of_done": "Fix the issue with a minimal source patch.",
        },
    )

    await ctx.phase("evidence")
    evidence_reports = await ctx.parallel(
        [
            lambda: ctx.agent(
                CONTRACT_MINER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=CONTRACT_MINER_SCHEMA,
                label="contract-miner",
                tools=_read_tools(),
            ),
            lambda: ctx.agent(
                TEST_CARTOGRAPHER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=TEST_CARTOGRAPHY_SCHEMA,
                label="test-cartographer",
                tools=_read_tools(),
            ),
            lambda: ctx.agent(
                CONTRACT_INVENTORY_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=OBSERVABLE_INVENTORY_SCHEMA,
                label="observable-contract-inventory",
                tools=_read_tools(),
            ),
        ]
    )
    contract_miner = _dict_or(
        evidence_reports[0] if evidence_reports else None,
        {"contracts": []},
    )
    cartography = _dict_or(
        evidence_reports[1] if len(evidence_reports) > 1 else None,
        {
            "framework": "",
            "runner_commands": [],
            "test_files": [],
            "fixtures": [],
            "temporary_test_guidance": "No structured cartography was produced.",
        },
    )
    inventory = _dict_or(
        evidence_reports[2] if len(evidence_reports) > 2 else None,
        {
            "observable_fields": [],
            "unaffected_fields": [],
            "risky_fields": [],
            "notes": "No structured inventory was produced.",
        },
    )

    await ctx.phase("contract-tribunal")
    tribunal = await _run_contract_tribunal(
        ctx,
        goal=goal,
        localization=localization,
        contracts=contract_miner,
        cartography=cartography,
        inventory=inventory,
        feedback="",
    )

    await ctx.phase("pre-validate")
    pre_candidates, pre_judge, baseline_triage = await _run_pre_patch_validation(
        ctx,
        goal=goal,
        localization=localization,
        tribunal=tribunal,
        cartography=cartography,
    )

    attempts: list[dict[str, Any]] = []
    feedback = ""
    for attempt in range(1, MAX_CODER_ROUNDS + 1):
        await ctx.phase(f"solve:r{attempt}")
        report = await _run_attempt(
            ctx,
            goal=goal,
            localization=localization,
            cartography=cartography,
            tribunal=tribunal,
            pre_judge=pre_judge,
            baseline_triage=baseline_triage,
            inventory=inventory,
            attempt=attempt,
            feedback=feedback,
        )
        attempts.append(report)

        if report["status"] == "done":
            return {
                "status": "done",
                "rounds": attempt,
                "contracts": len(_dict_or(tribunal, {"contracts": []}).get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.budget.spent(),
            }

        feedback = _feedback(report["final_verdict"], report.get("skeptic", {}), report.get("post_judge", {}))
        if report["status"] == "contract_rearbitrate" and attempt < MAX_CODER_ROUNDS:
            await ctx.phase("contract-tribunal")
            tribunal = await _run_contract_tribunal(
                ctx,
                goal=goal,
                localization=localization,
                contracts=contract_miner,
                cartography=cartography,
                inventory=inventory,
                feedback=feedback,
            )
            await ctx.phase("pre-validate")
            pre_candidates, pre_judge, baseline_triage = await _run_pre_patch_validation(
                ctx,
                goal=goal,
                localization=localization,
                tribunal=tribunal,
                cartography=cartography,
                label_suffix=f"rearbitrate-r{attempt}",
            )

        if attempt == MAX_CODER_ROUNDS:
            break

    return {
        "status": "incomplete",
        "rounds": MAX_CODER_ROUNDS,
        "contracts": len(_dict_or(tribunal, {"contracts": []}).get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "post_validation_accepted": _accepted_count(attempts[-1]["post_judge"]) if attempts else 0,
        "allowed_patch_paths": attempts[-1]["final_verdict"].get("allowed_patch_paths", []) if attempts else [],
        "disallowed_patch_paths": attempts[-1]["final_verdict"].get("disallowed_patch_paths", []) if attempts else [],
        "attempts": attempts,
        "tokens_spent": ctx.budget.spent(),
    }
