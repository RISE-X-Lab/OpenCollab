"""split-solve — analyze, split into independent subtasks, solve each with a
coder/tester loop, then synthesize and verify the combined result.

Sibling of ``self_collab.py``, same deterministic GAN-style coder/tester loop,
with three structural differences:

* the analyst splits the goal into INDEPENDENT subtasks (disjoint file sets)
  rather than sequential phases that build on each other;
* a failed subtask never blocks the remaining ones — independence means there
  is no "broken base" to stop on;
* a final synthesizer agent inspects the combined working-tree diff, runs the
  full test suite, repairs integration seams, and reports a structured verdict.

Subtasks still run sequentially: every agent shares ONE working tree (session
``isolation`` is currently a no-op in the factory), so concurrent coders would
race on files and test runs. When worktree isolation lands, the solve loop is
the one spot to parallelize.

Run:
    opencollab workflow run split-solve --args '{"goal": "<task>"}' [-w DIR]

The eval harness can run it unchanged: ``goal`` falls back to the task
``description`` that ``run_eval_task`` passes in its args dict.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.workflow_registry import workflow

MAX_ROUNDS_PER_SUBTASK = 3

# Shared rules — every role gets them (same contract as self_collab).
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

SPLIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "subtasks"],
    "properties": {
        "root_cause": {"type": "string"},
        "subtasks": {
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

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "remaining_issues"],
    "properties": {
        "status": {"type": "string", "enum": ["clean", "issues"]},
        "summary": {"type": "string"},
        "remaining_issues": {"type": "string", "description": "Empty when status is clean."},
    },
}

ANALYST_PROMPT = """\
You are the Analyst. Analyze ONLY — do not edit anything.
Read the goal and the codebase (file_read, grep; bash only for a one-line \
`python -c` behavior trace). Find the root cause, then split the work into \
INDEPENDENT subtasks: each is one self-contained unit with its own focused \
file set and a concrete, testable definition of done. No subtask may depend \
on another's edits — prefer DISJOINT file sets; subtasks must be valid in any \
order. Size subtasks by the actual work — do not pad or over-split; a single \
subtask is fine when the fix is atomic.

{rules}

Goal:
{goal}"""

CODER_PROMPT = """\
You are a Coder doing ONE subtask of a larger goal. The working tree may \
already contain edits from other subtasks — leave them alone; change only \
what YOUR subtask needs. Inspect with file_read/grep. Default edit: \
file_write in str_replace mode — minimal and targeted. If str_replace fails \
twice (no unique match — whitespace diff, duplicate/ambiguous lines, line \
drift), do NOT retry the same replacement: fall back to apply_patch with a \
content-anchored diff (use line_replace with expected_str to guard the range). \
Verify with run_tests (or a short `python -c` repro) before reporting. Your \
final message is your report: what you changed (each file + edit), why, and \
your verification result.

{rules}

Subtask goal:
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
You are a Tester adversarially verifying a coder's change for ONE subtask. \
Run the project's tests with run_tests. Inspect the ACTUAL source with \
file_read/grep — do not trust the coder's summary; confirm the change is \
really there and really meets the definition of done. The tree may also hold \
edits from other subtasks — judge only THIS subtask, but report any \
regression you see. You do not edit files.

Verdict PASS only when the change is really there and the definition of done \
holds. Verdict FAIL for a code defect. Verdict BLOCKED only when the failure is \
ENVIRONMENTAL — a missing dependency, no network, or broken/unrelated infra — \
not something more coding can fix; name the blocker in findings so it can be \
surfaced upward instead of burning more rounds.

{rules}

Subtask goal:
{goal}

Definition of done:
{done}

Coder's report:
{summary}"""

SYNTH_PROMPT = """\
You are the Synthesizer. The subtasks below were attempted against one shared \
working tree. Integrate and verify the COMBINED change against the original \
goal:
1. Run `git diff` (bash) to see the full change; read any touched file you \
need with file_read.
2. Run the project's tests with run_tests — the full suite, not a slice.
3. If the combined change has integration seams (conflicting edits, or a \
failed subtask leaving the tree broken), repair them with minimal, targeted \
edits.
4. Finish with a structured verdict: status "clean" when the goal is met and \
tests pass, else "issues" with what remains.

{rules}

Original goal:
{goal}

Subtask reports:
{reports}"""


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


async def _run_subtask(ctx: Any, sub: dict[str, Any], idx: int) -> dict[str, Any]:
    """Drive one subtask through the coder -> tester loop. Returns a report."""
    findings = ""
    rounds = 0
    for round_no in range(1, MAX_ROUNDS_PER_SUBTASK + 1):
        rounds = round_no
        findings_block = FINDINGS_BLOCK.format(findings=findings) if findings else ""
        summary = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=sub["goal"],
                files="\n".join(sub["files"]) or "(analyst did not pin files — keep the change minimal)",
                done=sub["done"],
                findings_block=findings_block,
            ),
            label=f"coder:s{idx}r{round_no}",
            tools=_coder_tools(),
        )
        verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=sub["goal"],
                done=sub["done"],
                summary=summary or "(coder died — verify the working tree yourself)",
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:s{idx}r{round_no}",
            tools=_tester_tools(),
        )
        if isinstance(verdict, dict) and verdict.get("verdict") == "PASS":
            return {"goal": sub["goal"], "status": "passed", "rounds": rounds}
        if isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED":
            # Environmental blocker: more coding rounds cannot clear it. Stop the
            # loop and surface the blocker upward instead of burning the rest.
            blocker = verdict.get("findings", "") or "environmental blocker (unspecified)"
            await ctx.log(f"subtask {idx} round {round_no} BLOCKED: {blocker[:200]}")
            return {"goal": sub["goal"], "status": "blocked", "rounds": rounds, "blocker": blocker}
        if not isinstance(verdict, dict):
            await ctx.log(f"subtask {idx} round {round_no} tester died — substituting generic findings")
        # Never re-issue an identical task: the next round carries the findings.
        findings = (
            verdict.get("findings", "") if isinstance(verdict, dict) else ""
        ) or "Tester returned no verdict. Re-verify the definition of done yourself before reporting."
        await ctx.log(f"subtask {idx} round {round_no} FAILED: {findings[:200]}")
    return {"goal": sub["goal"], "status": "failed", "rounds": rounds, "last_findings": findings}


@workflow(
    name="split-solve",
    description="Analyze -> split into independent subtasks -> per-subtask "
    "coder/tester loop -> synthesize and verify the combined result",
    phases=["analyze", "solve", "synthesize"],
)
async def split_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # ``goal`` for CLI runs; ``description`` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" — pass --args \'{"goal": "..."}\''}

    await ctx.phase("analyze")
    plan = await ctx.agent(
        ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=SPLIT_SCHEMA,
        label="analyst",
        tools=_read_tools(),
    )
    if not isinstance(plan, dict) or not plan.get("subtasks"):
        return {"status": "error", "error": "analyst produced no usable subtask split"}

    await ctx.phase("solve")
    reports: list[dict[str, Any]] = []
    for idx, sub in enumerate(plan["subtasks"]):
        report = await _run_subtask(ctx, sub, idx)
        reports.append(report)
        # Independence is the contract: a failed/blocked subtask never blocks the rest.
        if report["status"] != "passed":
            await ctx.log(
                f"subtask {idx} {report['status']} after {report['rounds']} rounds — continuing with the rest"
            )

    # Synthesis polishes a tree with at least one good change in it; with zero
    # passes there is nothing to integrate, so don't spend an agent on it.
    synthesis: dict[str, Any] | None = None
    if any(r["status"] == "passed" for r in reports):
        await ctx.phase("synthesize")
        raw = await ctx.agent(
            SYNTH_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                reports=json.dumps(reports, indent=2),
            ),
            schema=SYNTHESIS_SCHEMA,
            label="synthesizer",
            tools=_coder_tools(),
        )
        synthesis = raw if isinstance(raw, dict) else None
    else:
        await ctx.log("no subtask passed — skipping synthesis")

    all_passed = bool(reports) and all(r["status"] == "passed" for r in reports)
    clean = isinstance(synthesis, dict) and synthesis.get("status") == "clean"
    return {
        "status": "done" if (all_passed and clean) else "incomplete",
        "root_cause": plan.get("root_cause", ""),
        "subtasks": reports,
        "subtasks_planned": len(plan["subtasks"]),
        "synthesis": synthesis,
        "tokens_spent": ctx.budget.spent(),
    }
