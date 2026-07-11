"""self-collab — deterministic translation of configs/team.self.collab.yaml.

The YAML team relies on the analyst LLM to drive the coder/tester GAN loop via
spawn_agent; here the loop is plain Python, so round caps, never-identical
retries, and stop-on-failed-phase are guaranteed rather than prompted.

Stages: analyze (structured plan) -> parallel plan review (two lenses, one
revision pass when rejected) -> per plan phase, a sequential coder -> tester
loop with the tester's findings folded into the next coder round.

Run:
    opencollab workflow run self-collab --args '{"goal": "<task>"}' [-w DIR]
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.workflow_registry import workflow

MAX_ROUNDS_PER_PHASE = 3

# Shared rules, lifted from the YAML team config — every role gets them.
SHARED_RULES = """\
Rules:
- Prefer your DEDICATED tool over bash: file_read/grep to inspect, run_tests \
to test, file_write/apply_patch to edit. Use bash ONLY for what no dedicated \
tool covers (e.g. a one-line `python -c` repro).
- Fix the ROOT CAUSE in the source; make the SMALLEST correct change.
- NEVER edit test files. NEVER run `git commit`; leave edits in the working tree.
- Never assume a package is available: confirm the repo already imports it \
(grep / check the manifest) before using it, and verify your own imports \
resolve before reporting done.
- Keep reports tight: <=8 lines — changed files + what changed, why, and the \
verification result. No preamble or postamble.
- Do NOT grep for a FAIL_TO_PASS test that does not exist yet — the task may \
require creating it; chasing a missing test wastes budget."""

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "phases"],
    "properties": {
        "root_cause": {"type": "string"},
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["goal", "files", "done"],
                "properties": {
                    "goal": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "done": {"type": "string"},
                },
            },
        },
    },
}

PLAN_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["approved", "feedback"],
    "properties": {
        "approved": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {
            "type": "string",
            "description": "On FAIL: the exact failing command, error/traceback, suspected file/line. "
            "On BLOCKED: name the environmental blocker (missing dependency, no network, "
            "broken/unrelated infra) — not a code defect — so it can be surfaced upward.",
        },
    },
}

ANALYST_PROMPT = """\
You are the Analyst. Analyze ONLY — do not edit anything.
Read the goal and the codebase (file_read, grep; bash only for a one-line \
`python -c` behavior trace). Find the root cause, then break the work into \
sequential phases, each exactly ONE unit of work with a focused file set and a \
concrete, testable definition of done. Size phases by the actual work — do not \
pad or over-split; one phase is often enough.

{rules}

Goal:
{goal}"""

REVIEWER_PROMPT = """\
You are reviewing an implementation plan before any code is written. Verify it \
against the ACTUAL codebase with file_read/grep — do not trust the plan's claims.
Your lens: {lens}

Approve only if the plan survives your lens; otherwise set approved=false with \
specific, actionable feedback (file/line where possible).

Goal:
{goal}

Plan:
{plan}"""

REVIEW_LENSES = (
    "correctness — is the stated root cause the real one, and does each phase's "
    "definition of done actually verify the goal?",
    "scope — is the file set minimal and complete, are the phases properly "
    "sequenced, and is anything missing or unnecessary?",
)

REVISE_PROMPT = """\
You are the Analyst. Reviewers rejected your plan. Revise it — same format, \
addressing every point below. Re-check the codebase where the feedback disputes \
your reading of it.

{rules}

Goal:
{goal}

Previous plan:
{plan}

Reviewer feedback:
{feedback}"""

CODER_PROMPT = """\
You are a Coder doing ONE task. Inspect with file_read/grep. Default edit: \
file_write in str_replace mode — minimal and targeted. If str_replace fails \
twice (no unique match — whitespace diff, duplicate/ambiguous lines, line \
drift), do NOT retry the same replacement: fall back to apply_patch with a \
content-anchored diff (use line_replace with expected_str to guard the range). \
Verify with run_tests (or a short `python -c` repro) before reporting. Your \
final message is your report: what you changed (each file + edit), why, and \
your verification result.

{rules}

Task goal:
{goal}

Files to touch:
{files}

Definition of done:
{done}
{findings_block}"""

FINDINGS_BLOCK = """
A previous attempt FAILED verification. Do not repeat it — address these \
concrete findings from the tester:
{findings}"""

TESTER_PROMPT = """\
You are a Tester adversarially verifying a coder's change. Run the project's \
tests with run_tests. Inspect the ACTUAL source with file_read/grep — do not \
trust the coder's summary; confirm the change is really there and really fixes \
the root cause. Hunt failures: edge cases, missing handling, regressions in \
neighboring behavior. You do not edit files.

Verdict PASS only when the change is really there and the definition of done \
holds. Verdict FAIL for a code defect. Verdict BLOCKED only when the failure is \
ENVIRONMENTAL — a missing dependency, no network, or broken/unrelated infra — \
not something more coding can fix; name the blocker in findings so it can be \
surfaced upward instead of burning more rounds.

{rules}

Task goal:
{goal}

Definition of done:
{done}

Coder's report:
{summary}"""


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


async def _run_phase(ctx: Any, ph: dict[str, Any], idx: int) -> dict[str, Any]:
    """Drive one plan phase through the coder -> tester loop. Returns a report."""
    findings = ""
    rounds = 0
    for round_no in range(1, MAX_ROUNDS_PER_PHASE + 1):
        rounds = round_no
        findings_block = FINDINGS_BLOCK.format(findings=findings) if findings else ""
        summary = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=ph["goal"],
                files="\n".join(ph["files"]) or "(analyst did not pin files — keep the change minimal)",
                done=ph["done"],
                findings_block=findings_block,
            ),
            label=f"coder:p{idx}r{round_no}",
            tools=_coder_tools(),
        )
        verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=ph["goal"],
                done=ph["done"],
                summary=summary or "(coder died — verify the working tree yourself)",
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:p{idx}r{round_no}",
            tools=_tester_tools(),
        )
        if isinstance(verdict, dict) and verdict.get("verdict") == "PASS":
            return {"goal": ph["goal"], "status": "passed", "rounds": rounds}
        if isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED":
            # Environmental blocker: more coding rounds cannot clear it. Stop the
            # loop and surface the blocker upward instead of burning the rest.
            blocker = verdict.get("findings", "") or "environmental blocker (unspecified)"
            await ctx.log(f"phase {idx} round {round_no} BLOCKED: {blocker[:200]}")
            return {"goal": ph["goal"], "status": "blocked", "rounds": rounds, "blocker": blocker}
        if not isinstance(verdict, dict):
            await ctx.log(f"phase {idx} round {round_no} tester died — substituting generic findings")
        # Never re-issue an identical task: the next round carries the findings.
        findings = (
            verdict.get("findings", "") if isinstance(verdict, dict) else ""
        ) or "Tester returned no verdict. Re-verify the definition of done yourself before reporting."
        await ctx.log(f"phase {idx} round {round_no} FAILED: {findings[:200]}")
    return {"goal": ph["goal"], "status": "failed", "rounds": rounds, "last_findings": findings}


@workflow(
    name="self-collab",
    description="Analyze -> parallel plan review -> per-phase coder/tester GAN loop "
    "(deterministic port of configs/team.self.collab.yaml)",
    phases=["analyze", "review", "implement"],
)
async def self_collab(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal", "")).strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" — pass --args \'{"goal": "..."}\''}

    await ctx.phase("analyze")
    plan = await ctx.agent(
        ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=PLAN_SCHEMA,
        label="analyst",
        tools=_read_tools(),
    )
    if not isinstance(plan, dict) or not plan.get("phases"):
        return {"status": "error", "error": "analyst produced no usable plan"}

    await ctx.phase("review")
    plan_json = json.dumps(plan, indent=2)
    reviews = await ctx.parallel(
        [
            (
                lambda lens=lens, i=i: ctx.agent(
                    REVIEWER_PROMPT.format(lens=lens, goal=goal, plan=plan_json),
                    schema=PLAN_REVIEW_SCHEMA,
                    label=f"reviewer:{i}",
                    tools=_read_tools(),
                )
            )
            for i, lens in enumerate(REVIEW_LENSES)
        ]
    )
    # A reviewer that returns None counts as silent approval; surface the ratio
    # so a missing verdict is visible rather than silently absorbed.
    usable = sum(1 for r in reviews if isinstance(r, dict))
    if usable < len(reviews):
        await ctx.log(f"plan review: {usable}/{len(reviews)} verdicts usable")
    rejections = [
        r["feedback"]
        for r in reviews
        if isinstance(r, dict) and not r.get("approved") and r.get("feedback")
    ]
    if rejections:
        await ctx.log(f"plan rejected by {len(rejections)} reviewer(s) — revising")
        revised = await ctx.agent(
            REVISE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                plan=plan_json,
                feedback="\n---\n".join(rejections),
            ),
            schema=PLAN_SCHEMA,
            label="analyst:revise",
            tools=_read_tools(),
        )
        if isinstance(revised, dict) and revised.get("phases"):
            plan = revised

    await ctx.phase("implement")
    phase_reports: list[dict[str, Any]] = []
    for idx, ph in enumerate(plan["phases"]):
        report = await _run_phase(ctx, ph, idx)
        phase_reports.append(report)
        if report["status"] != "passed":
            # Stop rather than build later phases on a broken (or blocked) base.
            await ctx.log(f"phase {idx} {report['status']} after {report['rounds']} rounds — stopping")
            break

    all_passed = bool(phase_reports) and all(r["status"] == "passed" for r in phase_reports)
    return {
        "status": "done" if all_passed else "incomplete",
        "root_cause": plan.get("root_cause", ""),
        "plan_revised": bool(rejections),
        "phases": phase_reports,
        "phases_planned": len(plan["phases"]),
        "tokens_spent": ctx.budget.spent(),
    }
