"""scout-solve — analyze the problem into exploration dimensions, fan those out
to parallel read-only scouts, synthesize their findings into one solution brief,
then solve with a coder/tester loop.

Sibling of ``split_solve.py`` and ``self_collab.py``: same deterministic
GAN-style coder/tester loop, but a different front half. Where ``split_solve``
fans out the *solving* into independent subtasks, ``scout_solve`` fans out the
*understanding* — it pays for breadth of reconnaissance up front so the single
solve loop starts from a confirmed root cause instead of guessing.

Shape:

* the analyst does NOT plan a fix; it decomposes the PROBLEM into independent
  exploration dimensions (distinct angles — origin of the bug, how the subsystem
  works, what the tests/spec expect, who the callers/contracts are, edge cases);
* each dimension is investigated in parallel by a read-only scout that answers
  one focused question with file/line evidence — scouts never edit;
* a synthesizer reconciles every scout report into ONE actionable brief (root
  cause, approach, files, definition of done, watch-outs), confirming anything
  decisive against the source itself;
* a single coder/tester loop solves the goal from that brief, the tester's
  findings folded into the next coder round.

Scouting is read-only, so the parallel fan-out is safe even though every agent
shares one working tree (session ``isolation`` is currently a no-op). Only the
final solve loop mutates files, and it runs sequentially.

Run:
    opencollab workflow run scout-solve --args '{"goal": "<task>"}' [-w DIR]

The eval harness can run it unchanged: ``goal`` falls back to the task
``description`` that ``run_eval_task`` passes in its args dict.
"""

from __future__ import annotations

from typing import Any

from opencollab.sdk import (
    ApplyPatchTool,
    BashTool,
    FileReadTool,
    FileWriteTool,
    GrepTool,
    verification_run_tests_tool,
    workflow,
)

MAX_SOLVE_ROUNDS = 3

# Shared rules — every role gets them (same contract as the sibling workflows).
SHARED_RULES = """\
Rules:
- Prefer your DEDICATED tool over bash: file_read/grep to inspect, run_tests \
to test, file_write/apply_patch to edit. Use bash ONLY for what no dedicated \
tool covers (e.g. a one-line `python -c` repro).
- NEVER edit test files. NEVER run `git commit`; leave edits in the working tree.
"""

DIMENSIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["dimensions"],
    "properties": {
        "initial_read": {
            "type": "string",
            "description": "One or two sentences on your first read of the problem — optional context for the scouts.",
        },
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["aspect", "question", "hints"],
                "properties": {
                    "aspect": {"type": "string", "description": "Short name for this angle, e.g. 'bug origin'."},
                    "question": {
                        "type": "string",
                        "description": "The concrete, independently-answerable question this scout must resolve.",
                    },
                    "hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Where to start looking — files, dirs, symbols (may be empty).",
                    },
                },
            },
        },
    },
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "approach", "files", "done"],
    "properties": {
        "root_cause": {"type": "string"},
        "approach": {"type": "string", "description": "The smallest correct fix the scouting supports."},
        "files": {"type": "array", "items": {"type": "string"}},
        "done": {"type": "string", "description": "A concrete, testable definition of done."},
        "watch_outs": {
            "type": "string",
            "description": "Edge cases, contracts, or risks the scouts surfaced. Empty when none.",
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

ANALYST_PROMPT = """\
You are the Analyst. Do NOT solve and do NOT plan a fix — your job is to frame \
the investigation. Read the goal and skim the codebase (file_read, grep; bash \
only for a one-line `python -c` behavior trace) just enough to decompose the \
PROBLEM into INDEPENDENT exploration dimensions: distinct angles that, \
investigated in parallel, surface everything needed to solve it correctly — \
e.g. where the defect originates, how the relevant subsystem actually works, \
what the tests/spec expect, what callers and contracts depend on it, and the \
edge cases. Each dimension is ONE focused, read-only question with a hint about \
where to start. Dimensions must be answerable independently and in any order — \
no scout should need another's result. Size by the actual problem: do not pad \
or over-split; a couple of sharp dimensions beat many shallow ones.

{rules}

Goal:
{goal}"""

EXPLORER_PROMPT = """\
You are a Scout investigating ONE dimension of a larger problem. You do NOT \
edit anything — this is read-only reconnaissance (file_read, grep; bash only \
for a one-line `python -c` trace). Answer your dimension's question thoroughly \
and concretely: cite exact files and line numbers, quote the code that matters, \
and spell out the contracts, edge cases, and risks you find. Do not propose a \
full fix — surface the evidence the solver will need. Your final message IS your \
findings report: dense, specific, and backed by what you actually read.

{rules}

Overall goal (for context only — answer your dimension, not the whole goal):
{goal}

Your dimension — {aspect}:
{question}

Where to start:
{hints}"""

SYNTH_PROMPT = """\
You are the Synthesizer. Several scouts investigated the problem from different \
angles; their findings are below. Distill them into ONE actionable solution \
brief for the coder: the confirmed root cause, the recommended approach (the \
smallest correct change), the exact files to touch, a concrete testable \
definition of done, and any watch-outs the scouting surfaced. Reconcile \
conflicting reports rather than averaging them; if a decisive claim is \
unverified, confirm it yourself with file_read/grep before committing it to the \
brief. Do NOT edit anything.

{rules}

Goal:
{goal}

Scout findings:
{findings}"""

CODER_PROMPT = """\
You are a Coder. A scouting pass already mapped this problem — work from the \
brief below; trust it but verify anything decisive in the source before you \
rely on it. Inspect with file_read/grep. Default edit: file_write in \
str_replace mode — minimal and targeted. If str_replace fails twice (no unique \
match — whitespace diff, duplicate/ambiguous lines, line drift), do NOT retry \
the same replacement: fall back to apply_patch with a content-anchored diff \
(use line_replace with expected_str to guard the range). Verify with run_tests \
(or a short `python -c` repro) before reporting. Your final message is your \
report: what you changed (each file + edit), why, and your verification result.

{rules}

Goal:
{goal}

Brief from the scouting pass:
- Root cause: {root_cause}
- Approach: {approach}
- Definition of done: {done}
- Watch-outs: {watch_outs}

Files to touch:
{files}
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

Goal:
{goal}

Definition of done:
{done}

Coder's report:
{summary}"""


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        verification_run_tests_tool(),
        GrepTool(),
    ]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), verification_run_tests_tool(), GrepTool()]


async def _explore(ctx: Any, goal: str, dims: list[dict[str, Any]]) -> str:
    """Fan the dimensions out to parallel read-only scouts; return a combined,
    labelled findings document for the synthesizer."""
    reports = await ctx.parallel(
        [
            (
                lambda d=d, i=i: ctx.agent(
                    EXPLORER_PROMPT.format(
                        rules=SHARED_RULES,
                        goal=goal,
                        aspect=d.get("aspect", f"dimension {i}"),
                        question=d.get("question", ""),
                        hints="\n".join(d.get("hints") or []) or "(no starting point given — search from the goal)",
                    ),
                    label=f"scout:{i}:{(d.get('aspect') or '').strip().replace(' ', '-')[:24] or 'dim'}",
                    tools=_read_tools(),
                )
            )
            for i, d in enumerate(dims)
        ]
    )
    usable = sum(1 for r in reports if isinstance(r, str) and r.strip())
    if usable < len(reports):
        await ctx.log(f"exploration: {usable}/{len(reports)} scout reports usable")
    sections = []
    for i, (d, rep) in enumerate(zip(dims, reports)):
        body = rep if isinstance(rep, str) and rep.strip() else "(scout died — no findings for this dimension)"
        sections.append(f"## Dimension {i}: {d.get('aspect', '')}\nQuestion: {d.get('question', '')}\n\n{body}")
    return "\n\n".join(sections)


async def _solve(ctx: Any, goal: str, brief: dict[str, Any]) -> dict[str, Any]:
    """Drive the goal through the coder -> tester loop, guided by the brief."""
    files = "\n".join(brief.get("files") or []) or "(brief did not pin files — keep the change minimal)"
    findings = ""
    rounds = 0
    for round_no in range(1, MAX_SOLVE_ROUNDS + 1):
        rounds = round_no
        findings_block = FINDINGS_BLOCK.format(findings=findings) if findings else ""
        summary = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                root_cause=brief.get("root_cause", ""),
                approach=brief.get("approach", ""),
                done=brief.get("done", ""),
                watch_outs=brief.get("watch_outs", "") or "(none noted)",
                files=files,
                findings_block=findings_block,
            ),
            label=f"coder:r{round_no}",
            tools=_coder_tools(),
        )
        verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                done=brief.get("done", ""),
                summary=summary or "(coder died — verify the working tree yourself)",
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:r{round_no}",
            tools=_tester_tools(),
        )
        if isinstance(verdict, dict) and verdict.get("verdict") == "PASS":
            return {"status": "passed", "rounds": rounds}
        if isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED":
            # Environmental blocker: more coding rounds cannot clear it. Stop the
            # loop and surface the blocker upward instead of burning the rest.
            blocker = verdict.get("findings", "") or "environmental blocker (unspecified)"
            await ctx.log(f"solve round {round_no} BLOCKED: {blocker[:200]}")
            return {"status": "blocked", "rounds": rounds, "blocker": blocker}
        if not isinstance(verdict, dict):
            await ctx.log(f"solve round {round_no} tester died — substituting generic findings")
        # Never re-issue an identical task: the next round carries the findings.
        findings = (
            verdict.get("findings", "") if isinstance(verdict, dict) else ""
        ) or "Tester returned no verdict. Re-verify the definition of done yourself before reporting."
        await ctx.log(f"solve round {round_no} FAILED: {findings[:200]}")
    return {"status": "failed", "rounds": rounds, "last_findings": findings}


@workflow(
    name="scout-solve",
    description="Analyze -> fan out parallel read-only exploration across dimensions -> "
    "synthesize a solution brief -> coder/tester loop",
    phases=["analyze", "explore", "synthesize", "solve"],
)
async def scout_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # ``goal`` for CLI runs; ``description`` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" — pass --args \'{"goal": "..."}\''}

    await ctx.phase("analyze")
    plan = await ctx.agent(
        ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=DIMENSIONS_SCHEMA,
        label="analyst",
        tools=_read_tools(),
    )
    if not isinstance(plan, dict) or not plan.get("dimensions"):
        return {"status": "error", "error": "analyst produced no usable exploration dimensions"}
    dims = plan["dimensions"]

    await ctx.phase("explore")
    findings_doc = await _explore(ctx, goal, dims)

    await ctx.phase("synthesize")
    brief = await ctx.agent(
        SYNTH_PROMPT.format(rules=SHARED_RULES, goal=goal, findings=findings_doc),
        schema=BRIEF_SCHEMA,
        label="synthesizer",
        tools=_read_tools(),
    )
    if not isinstance(brief, dict) or not brief.get("root_cause"):
        return {
            "status": "error",
            "error": "synthesizer produced no usable brief",
            "dimensions_planned": len(dims),
            "tokens_spent": ctx.budget.spent(),
        }

    await ctx.phase("solve")
    report = await _solve(ctx, goal, brief)

    return {
        "status": "done" if report["status"] == "passed" else "incomplete",
        "root_cause": brief.get("root_cause", ""),
        "approach": brief.get("approach", ""),
        "dimensions_planned": len(dims),
        "brief": brief,
        "solve": report,
        "tokens_spent": ctx.budget.spent(),
    }
