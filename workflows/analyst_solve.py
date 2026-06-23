"""analyst-solve — analyst-driven reconnaissance, then a phased coder/tester build.

Sibling of ``scout_solve.py`` and ``self_collab.py``. It grafts ``scout_solve``'s
parallel read-only reconnaissance onto ``self_collab``'s phased coder/tester loop,
but the ANALYST stays in charge end to end: it first decomposes the problem into
exploration dimensions, then — after the scouts report — designs the phased fix
itself instead of handing off to a separate synthesizer.

Built for hard tasks where a single shallow pass already failed. Three levers
distinguish it from the siblings:

* it pays for breadth of reconnaissance up front (parallel scouts), so the plan
  starts from a confirmed root cause rather than a guess;
* phases run BEST-EFFORT — a failed phase does not stop the run (it leaves its
  partial edits and the next phase continues), because a partial patch grades
  better than none;
* a budget floor guarantees output: before every expensive step it reserves
  headroom, and if the budget runs low it bails to a single ``forced-write``
  coder whose only job is to land a concrete edit, right or wrong.

Shape:

* analyst (scope) decomposes the PROBLEM into independent exploration dimensions;
* each dimension is investigated in parallel by a read-only scout;
* analyst (plan) synthesizes the findings into a root cause, an approach, and an
  ordered list of implementation phases;
* each phase runs a sequential coder -> tester loop, best-effort;
* a final whole-goal verification gets one repair round if the budget allows.

Run:
    opencollab workflow run analyst-solve --args '{"goal": "<task>"}' [-w DIR]

The eval harness runs it unchanged: ``goal`` falls back to the task
``description`` that ``run_eval_task`` passes in its args dict.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.workflow_registry import workflow

# Rounds a single phase gets before the run moves on (best-effort, no stop).
MAX_ROUNDS_PER_PHASE = 4
# Token headroom kept in reserve. Once the remaining budget drops below this, the
# run abandons further loops and spends the reserve on one forced-write coder so
# the working tree is never left empty. Size it for one full coder session.
RESERVE_TOKENS = 350_000

# Shared rules — every role gets them (lifted from configs/team.self.collab.yaml,
# the SWE-bench-tuned variant: it warns off chasing not-yet-existing tests).
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

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "approach", "phases"],
    "properties": {
        "root_cause": {"type": "string", "description": "The confirmed root cause the reconnaissance supports."},
        "approach": {"type": "string", "description": "The smallest correct fix the evidence supports."},
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["goal", "files", "done"],
                "properties": {
                    "goal": {"type": "string", "description": "ONE unit of work."},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "done": {"type": "string", "description": "A concrete, testable definition of done."},
                },
            },
        },
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "tests_run", "failed_count"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {
            "type": "string",
            "description": "On FAIL: the exact failing command, error/traceback, suspected file/line. "
            "On BLOCKED: name the environmental blocker (missing dependency, no network, "
            "broken/unrelated infra) — not a code defect — so it can be surfaced upward.",
        },
        "tests_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The exact test node-ids you actually executed with run_tests this "
            "verification — proof, not a label. For a graded task this MUST include every "
            "target (FAIL_TO_PASS) node-id you were given.",
        },
        "failed_count": {
            "type": "integer",
            "description": "How many of the tests you ran failed or errored (0 for a clean PASS). "
            "Read it straight off run_tests' Counts line; do not estimate.",
        },
    },
}

SCOPE_PROMPT = """\
You are the Analyst. Do NOT solve and do NOT plan a fix yet — your job here is to \
frame the investigation. Read the goal and skim the codebase (file_read, grep; \
bash only for a one-line `python -c` behavior trace) just enough to decompose the \
PROBLEM into INDEPENDENT exploration dimensions: distinct angles that, \
investigated in parallel, surface everything needed to solve it correctly — e.g. \
where the defect originates, how the relevant subsystem actually works, what the \
tests/spec expect, what callers and contracts depend on it, and the edge cases. \
Each dimension is ONE focused, read-only question with a hint about where to \
start. Dimensions must be answerable independently and in any order — no scout \
should need another's result. Size by the actual problem: a couple of sharp \
dimensions beat many shallow ones; aim for two to four.

{rules}

Goal:
{goal}
{target_tests}"""

# Surfaces the FAIL_TO_PASS node-ids the run is graded on WITHOUT echoing the
# tests' literal assertion values — naming a test's expected output invites
# overfitting to that one input. We hand over the node-ids + an instruction to
# read the behavior and fix the ROOT CAUSE for the whole class of inputs.
TARGET_TESTS_BLOCK = """
Target tests (graded on these — fix the ROOT CAUSE, do not overfit):
{ids}
These node-ids name the BEHAVIOR your fix must satisfy. Read each test to \
understand the behavior it checks, but do NOT special-case the test's literal \
assertion values — fix the underlying defect for the whole class of inputs so \
the behavior is correct in general, not just for these exact cases."""

SCOUT_PROMPT = """\
You are a Scout investigating ONE dimension of a larger problem. You do NOT edit \
anything — this is read-only reconnaissance (file_read, grep; bash only for a \
one-line `python -c` trace). Answer your dimension's question thoroughly and \
concretely: cite exact files and line numbers, quote the code that matters, and \
spell out the contracts, edge cases, and risks you find. Do not propose a full \
fix — surface the evidence the planner will need. Your final message IS your \
findings report: dense, specific, and backed by what you actually read.

{rules}

Overall goal (for context only — answer your dimension, not the whole goal):
{goal}

Your dimension — {aspect}:
{question}

Where to start:
{hints}"""

PLAN_PROMPT = """\
You are the Analyst, now designing the solution. Reconnaissance is complete — the \
scouts' findings are below. Synthesize them into a concrete plan: the confirmed \
root cause, the approach (the smallest correct change the evidence supports), and \
an ordered list of implementation phases. Each phase is exactly ONE unit of work \
with a focused file set and a concrete, testable definition of done. Size phases \
by the actual work — most fixes are a SINGLE phase; split into multiple only when \
the work has genuinely independent parts better implemented and verified \
separately. Trust the findings but confirm anything decisive against the source \
yourself (file_read/grep) before committing it to the plan. Do NOT edit anything. \
Every target test's behavior below MUST be covered by some phase's definition of \
done — but define done by the corrected behavior, not by the test's literal values.

{rules}

Goal:
{goal}
{target_tests}

Reconnaissance findings:
{findings}"""

CODER_PROMPT = """\
You are a Coder implementing ONE phase of a planned fix. A reconnaissance pass \
already mapped this problem; work from the context and phase below, but verify \
anything decisive in the source before you rely on it. Inspect with \
file_read/grep. Default edit: file_write in str_replace mode — minimal and \
targeted. If str_replace fails twice (no unique match — whitespace diff, \
duplicate/ambiguous lines, line drift), do NOT retry the same replacement: fall \
back to apply_patch with a content-anchored diff (use line_replace with \
expected_str to guard the range). Verify with run_tests (or a short `python -c` \
repro) before reporting. Your final message is your report: what you changed \
(each file + edit), why, and your verification result.

{rules}

Overall goal:
{goal}

Confirmed root cause:
{root_cause}

Overall approach:
{approach}

This phase — {phase_goal}

Files to touch:
{files}

Definition of done:
{done}
{target_tests}
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

Proof, not a label. If target tests are named below, you MUST run them with \
run_tests using those EXACT node-ids (pass them as the `target`) and report them \
in `tests_run`; report the failed/errored total in `failed_count` straight off \
run_tests' Counts line. NEVER self-certify with `python -c` or by eyeballing the \
source — only a real run_tests execution of the named node-ids counts. \
PASS requires that EVERY named target node-id appears in `tests_run`, is in the \
run's passed set, and `failed_count` is 0 (zero failed, zero errored).

Verdict PASS only when the change is really there, the named target tests pass \
with zero failures, and the definition of done holds. Verdict FAIL for a code \
defect (including any target test still failing). Verdict BLOCKED only when the \
failure is ENVIRONMENTAL — a missing dependency, no network, or broken/unrelated \
infra — not something more coding can fix; name the blocker in findings so it can \
be surfaced upward instead of burning more rounds.

{rules}

Goal:
{goal}
{target_tests}

Definition of done:
{done}

Coder's report:
{summary}"""

FORCED_PROMPT = """\
You are a Coder and the token budget is nearly exhausted — this is the LAST \
action of the run. STOP investigating. Based on the confirmed root cause, the \
approach, and whatever edits are already in the working tree, implement the \
single most likely correct fix RIGHT NOW. You MUST leave concrete edits in the \
working tree (file_write or apply_patch) — a reasonable attempt is far better \
than no patch at all. Do not run the full test suite; at most a quick \
`python -c` sanity check. Then report in <=5 lines.

{rules}

Goal:
{goal}

Confirmed root cause:
{root_cause}

Approach:
{approach}

Work already attempted (for context):
{progress}"""

# Whole-goal definition of done for the final verification pass.
FINAL_DONE = (
    "The issue described in the goal is resolved at its root cause; the named "
    "FAIL_TO_PASS target tests run green with zero failures; and existing and "
    "neighboring tests still pass (no regressions)."
)


def _target_tests_block(args: dict[str, Any]) -> str:
    """Render the FAIL_TO_PASS node-ids as a behavior hint (or empty string).

    Anti-overfit by construction: we surface only the node-ids — never the
    tests' literal assertion values — plus a fix-the-root-cause instruction.
    Empty when no ids were threaded in (CLI runs, non-SWE-bench tasks), so the
    prompts collapse back to their original shape.
    """
    ids = args.get("fail_to_pass") or []
    if not ids:
        return ""
    listed = "\n".join(f"- {i}" for i in ids)
    return TARGET_TESTS_BLOCK.format(ids=listed)


def _f2p_gate(verdict: Any, fail_to_pass: list[str]) -> str | None:
    """Hard-gate a tester PASS on the real FAIL_TO_PASS node-ids (D2).

    Returns ``None`` when the PASS may stand, or a findings string when it must
    be overridden to not-passed. The gate fires only when ``fail_to_pass`` is
    non-empty (injection succeeded); an empty list means the harness could not
    inject the tests, so the verdict is trusted as-is — preserving today's
    behavior. Defense in depth: even a PASS verdict must carry machine-checkable
    proof — ``failed_count == 0`` AND every required node-id present in
    ``tests_run`` — or it does not count.
    """
    if not fail_to_pass:
        return None  # nothing to inject -> bypass the gate
    if not isinstance(verdict, dict):
        return None  # not a PASS to override; the caller handles dead/FAIL verdicts
    failed = verdict.get("failed_count")
    if isinstance(failed, int) and failed > 0:
        return (
            f"Tester reported {failed} failed/errored test(s). The named FAIL_TO_PASS "
            "tests must run green with ZERO failures. Re-run the exact target node-ids "
            "with run_tests and fix the remaining failures."
        )
    ran = verdict.get("tests_run")
    ran_set = set(ran) if isinstance(ran, list) else set()
    missing = [nid for nid in fail_to_pass if nid not in ran_set]
    if missing:
        listed = ", ".join(missing)
        return (
            "These required FAIL_TO_PASS node-ids were not shown as executed in the "
            f"verification: {listed}. Run them with run_tests using the EXACT node-ids "
            "and ensure they pass with zero failures before reporting PASS."
        )
    return None


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


def _time_low(ctx: Any) -> bool:
    """True when the run is within the deadline margin (wall-clock-aware).

    Defensive: a ctx without ``time_low`` (unbounded CLI runs, older test stubs)
    reports False so behavior is unchanged where no deadline is wired.
    """
    time_low = getattr(ctx, "time_low", None)
    return bool(time_low()) if callable(time_low) else False


def _budget_ok(ctx: Any) -> bool:
    """True while there is BOTH enough token budget AND enough wall-clock time
    left for another full coder/tester step.

    Keeps ``RESERVE_TOKENS`` in hand so the run can always afford one final
    forced-write coder (``remaining()`` is ``inf`` for an unbounded budget), and
    bails early once ``ctx.time_low()`` reports the hard deadline is near so the
    reserve is spent on the forced write BEFORE the wall truncates the run (P7 /
    django-11564 — the edit was located but never written because forced-write
    only checked tokens).
    """
    return ctx.budget.remaining() > RESERVE_TOKENS and not _time_low(ctx)


async def _recon(ctx: Any, goal: str, dims: list[dict[str, Any]]) -> str:
    """Fan the dimensions out to parallel read-only scouts; return a combined,
    labelled findings document for the planning analyst."""
    reports = await ctx.parallel(
        [
            (
                lambda d=d, i=i: ctx.agent(
                    SCOUT_PROMPT.format(
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
        await ctx.log(f"recon: {usable}/{len(reports)} scout reports usable")
    sections = []
    for i, (d, rep) in enumerate(zip(dims, reports)):
        body = rep if isinstance(rep, str) and rep.strip() else "(scout died — no findings for this dimension)"
        sections.append(f"## Dimension {i}: {d.get('aspect', '')}\nQuestion: {d.get('question', '')}\n\n{body}")
    return "\n\n".join(sections)


async def _run_phase(
    ctx: Any,
    goal: str,
    root_cause: str,
    approach: str,
    ph: dict[str, Any],
    idx: int,
    target_tests: str = "",
    fail_to_pass: list[str] | None = None,
) -> dict[str, Any]:
    """Drive one plan phase through the coder -> tester loop, best-effort.

    Returns a report whose ``status`` is one of: passed, failed, blocked,
    ``budget_low`` — signalling the caller to stop and force a final write while
    the reserve is still intact — or ``empty_tree``: the final round ended with
    the working tree verifiably unchanged (a tester PASS was overridden), so the
    caller should trigger a forced write.
    """
    phase_goal = ph.get("goal", goal)
    files = "\n".join(ph.get("files") or []) or "(analyst did not pin files — keep the change minimal)"
    done = ph.get("done", FINAL_DONE)
    f2p = fail_to_pass or []
    findings = ""
    rounds = 0
    for round_no in range(1, MAX_ROUNDS_PER_PHASE + 1):
        if not _budget_ok(ctx):
            why = "deadline near" if _time_low(ctx) else "budget below reserve"
            await ctx.log(
                f"phase {idx}: {why} before round {round_no} — stopping for forced write"
            )
            return {"goal": phase_goal, "status": "budget_low", "rounds": rounds}
        rounds = round_no
        findings_block = FINDINGS_BLOCK.format(findings=findings) if findings else ""
        summary = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                root_cause=root_cause,
                approach=approach,
                phase_goal=phase_goal,
                files=files,
                done=done,
                target_tests=target_tests,
                findings_block=findings_block,
            ),
            label=f"coder:p{idx}r{round_no}",
            tools=_coder_tools(),
        )
        # Disambiguate a dead coder (None) from an empty-output coder (""): the
        # `or` idiom collapsed both, hiding which failure occurred. Pass distinct
        # context to the tester each way.
        if summary is None:
            await ctx.log(f"phase {idx} round {round_no}: coder died (no session result)")
            coder_summary = "(coder died — no session result; verify the working tree yourself)"
        elif not summary.strip():
            await ctx.log(f"phase {idx} round {round_no}: coder produced empty output")
            coder_summary = "(coder produced empty output; verify the working tree yourself)"
        else:
            coder_summary = summary
        verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=phase_goal,
                done=done,
                target_tests=target_tests,
                summary=coder_summary,
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:p{idx}r{round_no}",
            tools=_tester_tools(),
        )
        # Diff guard: a tester PASS must NOT stand if the working tree is
        # verifiably unchanged this round — no edit means nothing to pass. Seed
        # the next round so the coder is told it MUST write; on the final round
        # signal the run to force a write.
        tree = await ctx.tree_changed()
        passed = isinstance(verdict, dict) and verdict.get("verdict") == "PASS"
        if passed and tree is False:
            await ctx.log(
                f"phase {idx} round {round_no}: tester PASS overridden — working tree unchanged"
            )
            findings = (
                "No edit was made this round — the working tree is unchanged. "
                "You MUST call file_write or apply_patch to land a concrete edit."
            )
            await ctx.log(f"phase {idx} round {round_no} FAILED: {findings}")
            if round_no == MAX_ROUNDS_PER_PHASE:
                return {
                    "goal": phase_goal,
                    "status": "empty_tree",
                    "rounds": rounds,
                    "last_findings": findings,
                }
            continue
        # F2P gate (the real lever): a tester PASS must NOT stand unless the run
        # carries proof the named FAIL_TO_PASS tests actually went green —
        # failed_count == 0 AND every required node-id present in tests_run. Only
        # active when ids were injected (f2p non-empty); empty -> bypass,
        # preserving today's behavior. Mirrors the tree-unchanged override: seed
        # the next round's findings and continue, or fail on the final round.
        if passed:
            gate_findings = _f2p_gate(verdict, f2p)
            if gate_findings is not None:
                await ctx.log(
                    f"phase {idx} round {round_no}: tester PASS overridden — "
                    "FAIL_TO_PASS proof insufficient"
                )
                findings = gate_findings
                await ctx.log(f"phase {idx} round {round_no} FAILED: {findings[:200]}")
                if round_no == MAX_ROUNDS_PER_PHASE:
                    return {
                        "goal": phase_goal,
                        "status": "failed",
                        "rounds": rounds,
                        "last_findings": findings,
                    }
                continue
        if passed:
            return {"goal": phase_goal, "status": "passed", "rounds": rounds}
        if isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED":
            blocker = verdict.get("findings", "") or "environmental blocker (unspecified)"
            await ctx.log(f"phase {idx} round {round_no} BLOCKED: {blocker[:200]}")
            return {"goal": phase_goal, "status": "blocked", "rounds": rounds, "blocker": blocker}
        if not isinstance(verdict, dict):
            await ctx.log(f"phase {idx} round {round_no} tester died — substituting generic findings")
        # Never re-issue an identical task: the next round carries the findings.
        findings = (
            verdict.get("findings", "") if isinstance(verdict, dict) else ""
        ) or "Tester returned no verdict. Re-verify the definition of done yourself before reporting."
        await ctx.log(f"phase {idx} round {round_no} FAILED: {findings[:200]}")
    return {"goal": phase_goal, "status": "failed", "rounds": rounds, "last_findings": findings}


def _seconds_left(ctx: Any) -> float:
    """Wall-clock seconds left before the hard deadline; ``inf`` when unbounded.

    Defensive: a ctx without ``seconds_left`` (unbounded CLI runs, older test
    stubs) reports ``inf`` so no timeout is imposed where no deadline is wired.
    """
    seconds_left = getattr(ctx, "seconds_left", None)
    return float(seconds_left()) if callable(seconds_left) else float("inf")


async def _forced_final_write(
    ctx: Any, goal: str, root_cause: str, approach: str, progress: str, *, reason: str
) -> str:
    """Spend the reserved headroom on one coder that MUST land an edit.

    ``reason`` distinguishes the trigger in the log ("budget low" vs "empty tree
    after implement"). The coder runs with ``tool_choice="required"`` so the
    provider forces a tool call — the session layer falls back to "auto" once if
    the endpoint rejects "required".

    This is the last action of the run and its whole job is to GUARANTEE a patch
    lands before the hard wall, so it is hardened two ways (P7 timing gap):
    ``thinking=False`` forces reasoning off so the generation is fast and cannot
    blow the deadline margin even when the run-wide default is thinking-on
    (analyst-solve eval runs with OPENCOLLAB_THINKING=1); and ``timeout`` clamps
    the call to whatever wall-clock time is left, so a stalled call is cancelled
    inside the workflow — its on-disk edits survive — instead of being truncated
    by the outer wall (which lost django-11564).
    """
    await ctx.log(f"forced write: {reason} — landing a best-effort patch")
    return await ctx.agent(
        FORCED_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            root_cause=root_cause,
            approach=approach,
            progress=progress or "(no prior coder edits recorded)",
        ),
        label="coder:forced-write",
        tools=_coder_tools(),
        tool_choice="required",
        thinking=False,
        timeout=_seconds_left(ctx),
    )


@workflow(
    name="analyst-solve",
    description="Analyst decomposes the problem -> parallel read-only recon -> analyst designs "
    "a phased plan -> best-effort coder/tester loop per phase -> final verify, with a "
    "budget floor that guarantees a patch",
    phases=["scope", "recon", "plan", "implement", "verify"],
)
async def analyst_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # ``goal`` for CLI runs; ``description`` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" — pass --args \'{"goal": "..."}\''}

    # FAIL_TO_PASS node-ids the run is graded on (threaded by the harness). The
    # block surfaces the BEHAVIOR, never the tests' literal values — see
    # _target_tests_block. Empty for CLI / non-SWE-bench runs. The raw id list
    # drives the code-side hard gate (D2): when present (injection succeeded) a
    # tester PASS must carry proof those node-ids ran green; when empty the gate
    # is bypassed, preserving today's behavior.
    target_tests = _target_tests_block(args)
    fail_to_pass = list(args.get("fail_to_pass") or [])

    # Phase 1 — analyst frames the investigation.
    await ctx.phase("scope")
    scope = await ctx.agent(
        SCOPE_PROMPT.format(rules=SHARED_RULES, goal=goal, target_tests=target_tests),
        schema=DIMENSIONS_SCHEMA,
        label="analyst:scope",
        tools=_read_tools(),
    )
    dims = scope.get("dimensions") if isinstance(scope, dict) else None

    # Phase 2 — parallel reconnaissance (skipped gracefully if framing failed).
    await ctx.phase("recon")
    if dims:
        findings_doc = await _recon(ctx, goal, dims)
    else:
        await ctx.log("recon skipped — analyst produced no dimensions")
        findings_doc = "(reconnaissance skipped — proceed from the goal itself)"

    # Phase 3 — analyst designs the phased plan from the findings.
    await ctx.phase("plan")
    plan = await ctx.agent(
        PLAN_PROMPT.format(
            rules=SHARED_RULES, goal=goal, target_tests=target_tests, findings=findings_doc
        ),
        schema=PLAN_SCHEMA,
        label="analyst:plan",
        tools=_read_tools(),
    )
    if isinstance(plan, dict) and plan.get("phases"):
        root_cause = plan.get("root_cause", "")
        approach = plan.get("approach", "")
        phases = plan["phases"]
    else:
        # Degrade gracefully: still attempt the fix as one implicit phase rather
        # than abandoning the task with an empty patch.
        await ctx.log("planner produced no usable plan — falling back to a single implicit phase")
        root_cause, approach = "", ""
        phases = [{"goal": goal, "files": [], "done": FINAL_DONE}]

    # Phase 4 — implement phases best-effort; bail to forced write if budget drops.
    await ctx.phase("implement")
    phase_reports: list[dict[str, Any]] = []
    forced = False
    for idx, ph in enumerate(phases):
        report = await _run_phase(
            ctx, goal, root_cause, approach, ph, idx, target_tests, fail_to_pass
        )
        phase_reports.append(report)
        if report["status"] in ("budget_low", "empty_tree"):
            progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
            reason = "budget low" if report["status"] == "budget_low" else "empty tree after phase"
            await _forced_final_write(ctx, goal, root_cause, approach, progress, reason=reason)
            forced = True
            break
        # Best-effort: a failed/blocked phase does NOT stop the run.
        await ctx.log(f"phase {idx} {report['status']} after {report.get('rounds', 0)} round(s)")

    # P0-2 — forced write on an empty tree, independent of budget. Even when no
    # phase signalled budget_low/empty_tree, if every phase finished but the
    # working tree is still verifiably empty, land a best-effort patch before the
    # final verify rather than reporting "done" with no edit. ``None`` (no probe
    # wired) is treated as "cannot verify" and does NOT trigger a forced write.
    if not forced and (await ctx.tree_changed()) is False:
        progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
        await _forced_final_write(
            ctx, goal, root_cause, approach, progress, reason="empty tree after implement"
        )
        forced = True

    # Phase 5 — one whole-goal verification, with a single repair round if affordable.
    await ctx.phase("verify")
    final_verdict: dict[str, Any] | None = None
    repaired = False
    if not forced and _budget_ok(ctx):
        final_verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                done=FINAL_DONE,
                target_tests=target_tests,
                summary="\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports)),
            ),
            schema=VERDICT_SCHEMA,
            label="tester:final",
            tools=_tester_tools(),
        )
        if (
            isinstance(final_verdict, dict)
            and final_verdict.get("verdict") == "FAIL"
            and _budget_ok(ctx)
        ):
            await ctx.log("final verify FAILED — one repair round")
            repaired = True
            await ctx.agent(
                CODER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    root_cause=root_cause,
                    approach=approach,
                    phase_goal="Address the final verification failure across the whole change.",
                    files="(use the tester findings to locate the files)",
                    done=FINAL_DONE,
                    target_tests=target_tests,
                    findings_block=FINDINGS_BLOCK.format(findings=final_verdict.get("findings", "")),
                ),
                label="coder:repair",
                tools=_coder_tools(),
            )
            final_verdict = await ctx.agent(
                TESTER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    done=FINAL_DONE,
                    target_tests=target_tests,
                    summary="(post-repair re-check)",
                ),
                schema=VERDICT_SCHEMA,
                label="tester:final2",
                tools=_tester_tools(),
            )

    passed_phases = sum(1 for r in phase_reports if r["status"] == "passed")
    # "verified" requires not just a PASS label but the named FAIL_TO_PASS tests
    # green: when ids were injected, the final verdict must also clear the f2p
    # gate (failed_count == 0 AND every required node-id in tests_run). With no
    # ids (gate bypassed) this collapses to the bare verdict == PASS check,
    # preserving today's behavior.
    verified = (
        isinstance(final_verdict, dict)
        and final_verdict.get("verdict") == "PASS"
        and _f2p_gate(final_verdict, fail_to_pass) is None
    )
    self_reported_done = verified or (not forced and passed_phases == len(phases) and phases)

    # A run cannot be "done" unless the working tree actually changed. The probe
    # answers True/False when wired, or None when it cannot verify. On None we
    # keep the self-reported outcome but flag it as unverified so the caller
    # knows the success was not corroborated by a real diff.
    tree = await ctx.tree_changed()
    if self_reported_done and tree is False:
        await ctx.log("run marked incomplete — working tree is empty despite a PASS self-report")
        status = "incomplete"
    else:
        status = "done" if self_reported_done else "incomplete"

    result: dict[str, Any] = {
        "status": status,
        "root_cause": root_cause,
        "approach": approach,
        "phases_planned": len(phases),
        "phases_passed": passed_phases,
        "phases": phase_reports,
        "forced_final_write": forced,
        "repaired": repaired,
        "final_verdict": final_verdict,
        "tree_changed": tree,
        "tokens_spent": ctx.budget.spent(),
    }
    if tree is None:
        result["tree_unverified"] = True
    return result
