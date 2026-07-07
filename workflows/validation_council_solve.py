"""validation-council-solve - contract-led validation council for SWE tasks.

This workflow turns a SWE-style issue into a sequence of auditable artifacts:
localization, behavior contracts, repository test cartography, candidate
validation probes, judge decisions, baseline triage, coding, diff risk audit,
post-patch probes, and final verification.

It is designed for blind SWE-bench use. Roles may inspect only the issue text,
repository code, public tests, and public documentation. They must not rely on
official hidden tests, injected grader patches, or FAIL_TO_PASS node ids.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.workflow_registry import workflow

MAX_APPROVED_PRE_TESTS = 5
MAX_APPROVED_POST_TESTS = 4
MAX_CODER_ROUNDS = 2
EMPTY_POST_CANDIDATES = {
    "tests": [],
    "abstained": True,
    "rationale": "Post-patch validation skipped.",
}
EMPTY_POST_JUDGE = {
    "accepted": [],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "Post-patch validation skipped.",
}
EMPTY_POST_TRIAGE = {
    "classifications": [],
    "approved_brief": "Post-patch triage skipped.",
    "abstained": True,
}
EMPTY_DIFF_RISKS = {
    "risks": [],
    "summary": "Diff risk audit skipped.",
}

SHARED_RULES = """\
Rules:
- Use only the issue text, repository code, public tests, and public docs.
- Do not use hidden grader tests, official test patches, injected FAIL_TO_PASS
  node ids, or any task extra that reveals the grading suite.
- Prefer dedicated tools: file_read/grep for inspection, run_tests for tests,
  file_write/apply_patch for edits. Use bash only when no dedicated tool fits.
- Keep temporary validation outside the final diff. Do not edit tests unless the
  task explicitly asks for a test-only change.
- If a validation probe needs a temporary file, write it only under
  /tmp/opencollab-validation-* and remove it after use.
- Fix the source root cause with the smallest correct change.
- Never run git commit; leave edits in the working tree."""

LOCALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "root_cause_hypothesis",
        "files",
        "public_api",
        "uncertainties",
        "definition_of_done",
    ],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_hypothesis": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "public_api": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "definition_of_done": {"type": "string"},
    },
}

CONTRACT_SCHEMA: dict[str, Any] = {
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
                    "scope",
                    "behavior_kind",
                    "evidence",
                    "confidence",
                    "testability",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "scope": {"type": "string"},
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
        },
    },
}

TEST_CARTOGRAPHY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "runner_commands",
        "test_files",
        "fixtures",
        "assertion_style",
        "temporary_test_guidance",
    ],
    "properties": {
        "framework": {"type": "string"},
        "runner_commands": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "fixtures": {"type": "array", "items": {"type": "string"}},
        "assertion_style": {"type": "string"},
        "temporary_test_guidance": {"type": "string"},
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
                    "expected_on_base": {"type": "string", "enum": ["fail", "pass", "unknown"]},
                    "expected_on_patch": {"type": "string", "enum": ["pass", "unknown"]},
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

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["accepted", "rejected", "diagnostic", "validation_brief"],
    "properties": {
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
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "reason"],
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "validation_brief": {"type": "string"},
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
                        "enum": [
                            "base_fail_repro",
                            "base_pass_regression",
                            "patch_pass",
                            "patch_fail",
                            "invalid",
                            "weak",
                            "not_run",
                        ],
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
    "required": ["risks", "summary"],
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
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "allowed_patch_paths", "disallowed_patch_paths"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {
            "type": "string",
            "description": "Commands run, evidence observed, and remaining defect or blocker.",
        },
        "allowed_patch_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths from git diff --name-only that are legitimate source changes.",
        },
        "disallowed_patch_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Temporary validation files, tests, logs, caches, or other non-submission paths.",
        },
    },
}

LOCALIZER_PROMPT = """\
You are the Analyst / Localizer. Analyze only; do not edit files.
Identify the likely source area, public API, root-cause hypothesis, unknowns,
and definition of done. Read the repository and public tests for evidence.

{rules}

Goal:
{goal}"""

CONTRACT_MINER_PROMPT = """\
You are the Contract Miner. Extract behavior contracts only. A contract must be
grounded in issue text, source behavior, public docs, or public tests. Do not
write tests and do not infer exact hidden assertions.

For each contract, record whether it describes desired behavior, currently
buggy behavior, or existing unaffected behavior. Cite concrete evidence.

{rules}

Goal:
{goal}

Localization:
{localization}"""

TEST_CARTOGRAPHER_PROMPT = """\
You are the Test Cartographer. Map how this repository expresses tests: runner,
fixtures, assertion style, relevant public test files, and how temporary probes
can be run without entering the final diff. Do not solve the issue.

{rules}

Goal:
{goal}

Localization:
{localization}"""

PRE_VALIDATION_FACTORY_PROMPT = """\
You are the Pre-Patch Validation Factory. Propose candidate validation probes
before coding. Each candidate must link to behavior contract ids and evidence.
Prefer short repro, boundary, regression, or metamorphic probes. Mark weak or
diagnostic-only probes as such. Do not edit files.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}"""

JUDGE_PROMPT = """\
You are the Validation Judge / Prioritizer for the {stage} stage. Apply hard
evidence gates. Accept at most {cap} candidates. Reject a candidate if it lacks
contract ids, lacks concrete evidence, asserts behavior only from a proposed
implementation, or depends on hidden grader knowledge. Diagnostics may be kept
separate, but they must not block final acceptance.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Candidates:
{candidates}"""

BASELINE_TRIAGE_PROMPT = """\
You are the Baseline Executor and Triage role. Run only accepted validation
probes that are cheap and safe, using temporary files or one-shot commands that
do not enter the final diff. If a file is needed, use /tmp/opencollab-validation-*
only. Classify each accepted probe against the current base as base_fail_repro,
base_pass_regression, invalid, weak, or not_run. Record exact commands and
observations.

{rules}

Goal:
{goal}

Accepted validation:
{judge}"""

CODER_PROMPT = """\
You are the Coder. Implement a minimal source fix using the evidence package.
Do not edit tests unless the task explicitly requires test-only changes.
Run relevant public tests and accepted validation probes where practical.
Your final message should name changed source files, explain the root cause,
and summarize verification.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}

Pre-patch validation judge:
{pre_judge}

Baseline triage:
{baseline_triage}
{feedback_block}"""

FEEDBACK_BLOCK = """
Previous attempt feedback:
{feedback}"""

PATCH_VALIDATOR_PROMPT = """\
You are the Patch Validator. Do not edit files. Verify the current working tree
against the goal, public tests, accepted pre-patch validation, and baseline
triage. Run tests or focused probes when practical. Verdict PASS only when the
source change is present, minimal, and satisfies the approved validation. Run
`git diff --name-only`; put legitimate source paths in allowed_patch_paths and
all tests, temporary probes, caches, logs, notes, and generated artifacts in
disallowed_patch_paths.

{rules}

Goal:
{goal}

Coder report:
{coder_report}

Accepted validation:
{pre_judge}

Baseline triage:
{baseline_triage}"""

DIFF_RISK_PROMPT = """\
You are the Diff Risk Auditor. Do not edit files. Read the current git diff and
identify semantic risks, missed contracts, neighboring behavior that may
regress, and focused probes that would catch those risks.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Patch validator verdict:
{patch_verdict}"""

POST_VALIDATION_FACTORY_PROMPT = """\
You are the Post-Patch Validation Factory. Use the accepted contracts, current
diff risks, and public repository behavior to propose additional post-patch
probes. Do not derive assertions only from the implementation. Do not edit
files.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Diff risks:
{risks}"""

POST_TRIAGE_PROMPT = """\
You are the Post-Patch Validation Triage role. Run accepted post-patch probes
when cheap and safe. Keep temporary probes outside the final diff. Classify each
probe as patch_pass, patch_fail, invalid, weak, or not_run. If a file is needed,
use /tmp/opencollab-validation-* only. Report exact commands and observations.

{rules}

Goal:
{goal}

Accepted post-patch validation:
{judge}"""

FINAL_VERIFIER_PROMPT = """\
You are the Final Verifier. Do not edit files. Inspect git diff, run relevant
public tests and approved validation where practical, and check that temporary
validation files are absent from the final diff. Run `git diff --name-only` and
place legitimate source changes in allowed_patch_paths. Place all tests,
temporary probes, caches, logs, notes, and generated artifacts in
disallowed_patch_paths, and fail if any disallowed path remains in the diff.
Verdict PASS only when the issue is fixed by source changes and the validation
evidence is clean.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Pre-patch validation:
{pre_judge}

Baseline triage:
{baseline_triage}

Coder report:
{coder_report}

Patch validator verdict:
{patch_verdict}

Diff risks:
{risks}

Post-patch validation:
{post_judge}

Post-patch triage:
{post_triage}"""


def _read_tools() -> list[Any]:
    return [FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]


def _tester_tools() -> list[Any]:
    return [
        FileReadTool(),
        RunTestsTool(allow_runner_override=False, allow_extra_args=False),
        GrepTool(),
        GitDiffTool(),
    ]


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _dict_or(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else fallback


def _trim_judge(judge: dict[str, Any], cap: int) -> dict[str, Any]:
    accepted = judge.get("accepted")
    if isinstance(accepted, list):
        judge = {**judge, "accepted": accepted[:cap]}
    return judge


def _accepted_count(judge: Any) -> int:
    if isinstance(judge, dict) and isinstance(judge.get("accepted"), list):
        return len(judge["accepted"])
    return 0


def _is_pass(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "PASS"


def _is_blocked(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED"


def _feedback(*reports: Any) -> str:
    parts: list[str] = []
    for report in reports:
        if isinstance(report, dict):
            text = report.get("findings") or report.get("approved_brief") or report.get("summary")
            if text:
                parts.append(str(text))
        elif isinstance(report, str) and report.strip():
            parts.append(report.strip())
    return "\n\n".join(parts) or "No structured feedback was returned; re-verify from the evidence package."


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    contracts: dict[str, Any],
    candidates: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await ctx.agent(
        JUDGE_PROMPT.format(
            rules=SHARED_RULES,
            stage=stage,
            cap=cap,
            goal=goal,
            contracts=_dump(contracts),
            candidates=_dump(candidates),
        ),
        schema=JUDGE_SCHEMA,
        label=f"{stage}-validation-judge",
        tools=_read_tools(),
    )
    return _trim_judge(
        _dict_or(
            judge,
            {
                "accepted": [],
                "rejected": [],
                "diagnostic": [],
                "validation_brief": "Judge returned no structured decision.",
            },
        ),
        cap,
    )


async def _run_attempt(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    contracts: dict[str, Any],
    cartography: dict[str, Any],
    pre_judge: dict[str, Any],
    baseline_triage: dict[str, Any],
    attempt: int,
    feedback: str,
) -> dict[str, Any]:
    feedback_block = FEEDBACK_BLOCK.format(feedback=feedback) if feedback else ""
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            cartography=_dump(cartography),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            feedback_block=feedback_block,
        ),
        label=f"coder:r{attempt}",
        tools=_coder_tools(),
    )
    patch_verdict = await ctx.agent(
        PATCH_VALIDATOR_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            coder_report=coder_report or "(coder returned no report)",
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
        ),
        schema=VERDICT_SCHEMA,
        label=f"patch-validator:r{attempt}",
        tools=_tester_tools(),
    )
    patch_verdict = _dict_or(
        patch_verdict,
        {
            "verdict": "FAIL",
            "findings": "Patch validator returned no structured verdict.",
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )
    if _is_blocked(patch_verdict):
        return {
            "attempt": attempt,
            "coder_report": coder_report or "",
            "patch_verdict": patch_verdict,
            "diff_risks": EMPTY_DIFF_RISKS,
            "post_candidates": EMPTY_POST_CANDIDATES,
            "post_judge": EMPTY_POST_JUDGE,
            "post_triage": EMPTY_POST_TRIAGE,
            "final_verdict": patch_verdict,
        }

    await ctx.phase(f"diff-risk:r{attempt}")
    risks = await ctx.agent(
        DIFF_RISK_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            contracts=_dump(contracts),
            patch_verdict=_dump(patch_verdict),
        ),
        schema=DIFF_RISK_SCHEMA,
        label=f"diff-risk-auditor:r{attempt}",
        tools=_tester_tools(),
    )
    risks = _dict_or(risks, {"risks": [], "summary": "Diff risk auditor returned no structured report."})

    post_candidates = await ctx.agent(
        POST_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            contracts=_dump(contracts),
            risks=_dump(risks),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"post-validation-factory:r{attempt}",
        tools=_read_tools(),
    )
    post_candidates = _dict_or(
        post_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured post-patch candidates."},
    )

    post_judge = await _judge_candidates(
        ctx,
        goal=goal,
        contracts=contracts,
        candidates=post_candidates,
        stage=f"post-r{attempt}",
        cap=MAX_APPROVED_POST_TESTS,
    )

    post_triage = await ctx.agent(
        POST_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(post_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label=f"post-validation-triage:r{attempt}",
        tools=_tester_tools(),
    )
    post_triage = _dict_or(
        post_triage,
        {"classifications": [], "approved_brief": "No post-patch triage.", "abstained": True},
    )

    await ctx.phase(f"final-verify:r{attempt}")
    final_verdict = await ctx.agent(
        FINAL_VERIFIER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            coder_report=coder_report or "(coder returned no report)",
            patch_verdict=_dump(patch_verdict),
            risks=_dump(risks),
            post_judge=_dump(post_judge),
            post_triage=_dump(post_triage),
        ),
        schema=VERDICT_SCHEMA,
        label=f"final-verifier:r{attempt}",
        tools=_tester_tools(),
    )
    final_verdict = _dict_or(
        final_verdict,
        {
            "verdict": "FAIL",
            "findings": "Final verifier returned no structured verdict.",
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )

    return {
        "attempt": attempt,
        "coder_report": coder_report or "",
        "patch_verdict": patch_verdict,
        "diff_risks": risks,
        "post_candidates": post_candidates,
        "post_judge": post_judge,
        "post_triage": post_triage,
        "final_verdict": final_verdict,
    }


@workflow(
    name="validation-council-solve",
    description="Blind contract-led SWE workflow with validation judges, diff risk audit, and capped retry",
    phases=["localize", "evidence", "pre-validate", "solve", "diff-risk", "final-verify"],
)
async def validation_council_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
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
            "files": [],
            "public_api": [],
            "uncertainties": ["localizer returned no structured output"],
            "definition_of_done": "Resolve the issue with a minimal source patch.",
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
                schema=CONTRACT_SCHEMA,
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
        ]
    )
    contracts = _dict_or(evidence_reports[0] if evidence_reports else None, {"contracts": []})
    cartography = _dict_or(
        evidence_reports[1] if len(evidence_reports) > 1 else None,
        {
            "framework": "",
            "runner_commands": [],
            "test_files": [],
            "fixtures": [],
            "assertion_style": "",
            "temporary_test_guidance": "No structured cartography was produced.",
        },
    )

    await ctx.phase("pre-validate")
    pre_candidates = await ctx.agent(
        PRE_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            cartography=_dump(cartography),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label="pre-validation-factory",
        tools=_read_tools(),
    )
    pre_candidates = _dict_or(
        pre_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured pre-patch candidates."},
    )
    pre_judge = await _judge_candidates(
        ctx,
        goal=goal,
        contracts=contracts,
        candidates=pre_candidates,
        stage="pre",
        cap=MAX_APPROVED_PRE_TESTS,
    )
    baseline_triage = await ctx.agent(
        BASELINE_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(pre_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label="baseline-triage",
        tools=_tester_tools(),
    )
    baseline_triage = _dict_or(
        baseline_triage,
        {"classifications": [], "approved_brief": "No baseline triage.", "abstained": True},
    )

    attempts: list[dict[str, Any]] = []
    feedback = ""
    for attempt in range(1, MAX_CODER_ROUNDS + 1):
        await ctx.phase(f"solve:r{attempt}")
        report = await _run_attempt(
            ctx,
            goal=goal,
            localization=localization,
            contracts=contracts,
            cartography=cartography,
            pre_judge=pre_judge,
            baseline_triage=baseline_triage,
            attempt=attempt,
            feedback=feedback,
        )
        attempts.append(report)
        if _is_pass(report["final_verdict"]):
            return {
                "status": "done",
                "rounds": attempt,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.budget.spent(),
            }
        if _is_blocked(report["final_verdict"]):
            blocker = report["final_verdict"].get("findings", "")
            await ctx.log(f"attempt {attempt} blocked: {blocker[:200]}")
            return {
                "status": "blocked",
                "rounds": attempt,
                "blocker": blocker,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.budget.spent(),
            }
        feedback = _feedback(
            report["final_verdict"],
            report["patch_verdict"],
            report["post_triage"],
            report["diff_risks"],
        )
        await ctx.log(f"attempt {attempt} failed: {feedback[:200]}")

    return {
        "status": "incomplete",
        "rounds": MAX_CODER_ROUNDS,
        "contracts": len(contracts.get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "post_validation_accepted": _accepted_count(attempts[-1]["post_judge"]) if attempts else 0,
        "allowed_patch_paths": attempts[-1]["final_verdict"].get("allowed_patch_paths", []) if attempts else [],
        "disallowed_patch_paths": attempts[-1]["final_verdict"].get("disallowed_patch_paths", []) if attempts else [],
        "attempts": attempts,
        "tokens_spent": ctx.budget.spent(),
    }
