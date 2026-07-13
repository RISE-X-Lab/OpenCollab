"""base-team workflow.

This is the Workflow-form version of the simple analyst/coder/tester Team
strategy. The cooperation logic is code-owned: analyst briefs the task, coder
edits, tester verifies, and tester findings can trigger a bounded repair round.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.sdk import (
    ApplyPatchTool,
    BashTool,
    FileReadTool,
    FileWriteTool,
    GrepTool,
    RunTestsTool,
    workflow,
)

MAX_REPAIR_ROUNDS = 2

SHARED_RULES = """\
Rules:
- Prefer file_read and grep for inspection.
- Prefer file_write or apply_patch for edits.
- Prefer run_tests for verification.
- Use bash only for short behavior checks or commands the dedicated tools do not cover.
- Fix the source root cause with a minimal patch.
- Never edit benchmark tests, never run git commit, and leave the patch in the working tree.
- Keep the final role report concise and evidence-based.
"""

ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "files", "implementation_task", "verification_task"],
    "properties": {
        "root_cause": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "implementation_task": {"type": "string"},
        "verification_task": {"type": "string"},
    },
}

TESTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {"type": "string"},
    },
}

ANALYST_PROMPT = """\
You are the Analyst in baseTeam. Analyze only; do not edit files.

Read the task and the repository. Identify the most likely root cause, the
small file set the Coder should touch, and a concrete verification task for the
Tester. If the issue mentions hidden benchmark tests, infer behavior from source
code and public tests.

{rules}

Task:
{goal}
"""

CODER_PROMPT = """\
You are the Coder in baseTeam. Complete one implementation task, then report.

{rules}

Task:
{goal}

Analyst brief:
{brief}

Tester findings from the previous round:
{findings}
"""

TESTER_PROMPT = """\
You are the Tester in baseTeam. Verify the Coder's patch adversarially.

Inspect the actual source changes and run the most relevant checks. Report
PASS only when the patch appears to satisfy the task and does not introduce an
obvious regression. Report BLOCKED only for an environment problem that stops
verification.

{rules}

Task:
{goal}

Analyst brief:
{brief}

Coder report:
{coder_report}
"""


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        RunTestsTool(),
        GrepTool(),
    ]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


@workflow(
    name="base-team",
    description="Simple analyst, coder, and tester cooperation strategy as a workflow.",
    phases=["analyze", "code", "verify"],
)
async def base_team(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("analyze")
    brief = await ctx.agent(
        ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=ANALYST_SCHEMA,
        label="baseTeam:analyst",
        tools=_read_tools(),
    )
    if not isinstance(brief, dict):
        return {
            "status": "error",
            "error": "analyst produced no structured brief",
            "tokens_spent": ctx.budget.spent(),
        }

    findings = ""
    rounds: list[dict[str, Any]] = []
    for round_no in range(1, MAX_REPAIR_ROUNDS + 2):
        await ctx.phase("code" if round_no == 1 else f"code-repair-{round_no}")
        coder_report = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                brief=_dump(brief),
                findings=findings or "No tester findings yet.",
            ),
            label=f"baseTeam:coder:{round_no}",
            tools=_coder_tools(),
        )
        source_changed = await ctx.source_changed()

        await ctx.phase("verify")
        tester_verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                brief=_dump(brief),
                coder_report=coder_report or "Coder produced no final report.",
            ),
            schema=TESTER_SCHEMA,
            label=f"baseTeam:tester:{round_no}",
            tools=_tester_tools(),
        )
        if not isinstance(tester_verdict, dict):
            tester_verdict = {
                "verdict": "FAIL",
                "findings": "Tester produced no structured verdict.",
            }

        rounds.append(
            {
                "round": round_no,
                "coder_report": coder_report or "",
                "source_changed": source_changed,
                "tester_verdict": tester_verdict,
            }
        )

        verdict = str(tester_verdict.get("verdict") or "").upper()
        if verdict == "PASS" and source_changed is not False:
            return {
                "status": "done",
                "root_cause": brief.get("root_cause", ""),
                "files": brief.get("files", []),
                "rounds": rounds,
                "tokens_spent": ctx.budget.spent(),
            }
        if verdict == "BLOCKED":
            return {
                "status": "blocked",
                "root_cause": brief.get("root_cause", ""),
                "files": brief.get("files", []),
                "rounds": rounds,
                "tokens_spent": ctx.budget.spent(),
            }
        findings = str(tester_verdict.get("findings") or "Tester reported failure.")

    return {
        "status": "incomplete",
        "root_cause": brief.get("root_cause", ""),
        "files": brief.get("files", []),
        "rounds": rounds,
        "tokens_spent": ctx.budget.spent(),
    }


__all__ = ["base_team"]
