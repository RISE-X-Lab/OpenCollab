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
from opencollab.application.fact_sheet import (
    build_fact_sheet,
    estimate_target_complexity,
    format_fact_sheet_hint,
    size_recon,
)
from opencollab.application.session_run import ENFORCEMENT_OFF
from opencollab.application.submit_findings import format_findings_report
from opencollab.application.workflow_registry import workflow

# Rounds a single phase gets before the run moves on (best-effort, no stop).
MAX_ROUNDS_PER_PHASE = 4
# Token headroom kept in reserve. Once the remaining budget drops below this, the
# run abandons further loops and spends the reserve on one forced-write coder so
# the working tree is never left empty. Sized for one forced write PLUS a final
# verify (FORCED_WRITE_BUDGET + TESTER_BUDGET), so verify is never starved.
RESERVE_TOKENS = 350_000

# Per-call token caps passed as ``budget=`` to each ctx.agent. Anchored on the
# real per-role spend measured from instrumented runs (healthy scouts 140-230k;
# scope/plan analysts ~90-130k; the implement coder needs the bulk). Each cap
# bounds a SINGLE runaway session — e.g. a non-converging scout that snowballs
# its context past 700k and drains the whole pool — without throttling a
# legitimately hard step; the framework clamps every cap to the live global
# remaining, so the shared pool is never overshot. Allocation is per CALL, not
# per role: analyst:scope and analyst:plan are the same role yet get separate
# caps, which is what lets us throttle the scope call (it snowballed to 400k)
# without touching plan.
SCOPE_BUDGET = 200_000
SCOUT_BUDGET = 250_000
PLAN_BUDGET = 150_000
CODER_BUDGET = 350_000
TESTER_BUDGET = 200_000
FORCED_WRITE_BUDGET = 120_000
REPAIR_BUDGET = 200_000
# Cap on parallel recon scouts so recon's total is bounded no matter how many
# dimensions the scope analyst invents.
MAX_SCOUTS = 4

# Budget the recon phase MUST leave untouched for the rest of the run (plan +
# implement/forced-write + verify). Recon scouts are read-only, so the
# reads_since_last_edit write-nudge never brakes them; each fills its cap
# exploring. With a FIXED per-scout cap their sum (SCOUT_BUDGET * MAX_SCOUTS ~=
# 1M) drains a 1M pool inside recon before implement/verify ever run — measured:
# recon ate 66-92% of a 1M budget, 7/8 instances ended with empty completions.
# Fix: derive the scout cap from the LIVE remaining minus this floor, so the
# scouts collectively can never dip below it:
#     scout_cap = min(SCOUT_BUDGET, (remaining - RECON_FLOOR) // n_scouts)
# At 2M the SCOUT_BUDGET ceiling binds and the tail is naturally safe; at 1M the
# floor binds and throttles scouts so plan/implement/verify always keep this
# reserve (which exceeds RESERVE_TOKENS, leaving room for plan + a forced write +
# a final verify). This is the "deduct recon, guarantee the tail" rule — it makes
# the steering hint's per-call-cap blindness harmless because recon is bounded.
#
# Raised 400k -> 600k so the implement loop's round-1 gate can actually FIRE:
# after recon leaves this floor and plan spends PLAN_BUDGET (150k), the tail keeps
# 600k - 150k = 450k > RESERVE_TOKENS (350k), so implement runs a real
# coder/tester round instead of ALWAYS bailing to a forced write (at 400k the tail
# was 400k-150k=250k < 350k -> gate never fired). This starves the measured
# ~91%-over-funded scouts from 150k -> 100k each (recon_pool 600k -> 400k at 1M),
# which they do not need (they re-read one core file 12-26x well under cap).
RECON_FLOOR = 600_000

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
- Keep reports free of preamble and postamble. A STATUS report (what changed, \
why, the verification result) stays under ~8 lines. But when your job is to \
surface EVIDENCE — a scout answering its dimension, or a coder citing exactly \
what it changed — give the next agent the full detail it needs: exact file \
paths, line numbers, and the quotes that matter. Never drop evidence to fit a \
line count.
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
{hints}{draft_block}"""

# STEP 5b commit-first: the bounded submit-only prompt that produces a scout's
# turn-0 DRAFT from the STATIC fact sheet alone (no reads). The draft anchors a
# committed cite-or-abstain artifact BEFORE exploration; the scout then revises it.
# Cite-or-abstain: fact-sheet-only, so every draft finding is verified=false until
# the scout's own read confirms it (NOT fabrication — an honest unconfirmed draft).
DRAFT_PROMPT = """\
You are a Scout about to investigate ONE dimension of a problem. BEFORE you read \
anything, commit a DRAFT of your findings based ONLY on the static fact sheet below — \
call submit_findings now. This draft anchors your investigation; you will revise it \
with real evidence in the next step.

Cite-or-abstain: the fact sheet is STATIC and NOT YET confirmed by your own reads, so \
mark EVERY draft finding verified=false and use lower confidence. You MAY set \
evidence_anchor to a location the fact sheet lists (e.g. a file:line) as a POINTER to \
check, but keep verified=false until one of your own reads confirms it. If the fact \
sheet is too thin to draft anything for this dimension, set insufficient_evidence=true \
— do NOT fabricate findings or anchors.

Your dimension — {aspect}:
{question}

Static fact sheet:
{fact_hint}

Where you will look next:
{hints}"""

# Appended to SCOUT_PROMPT when a draft was committed: frame the reads as REVISION
# of the committed draft (the scout's refined submit, not this draft, is harvested).
DRAFT_REVISE_BLOCK = """

Your committed draft (from the static fact sheet — a hypothesis, NOT a conclusion):
{draft}

Revise and STRENGTHEN this draft with real evidence: confirm each finding against the \
actual source with your reads and upgrade confirmed ones to verified=true with a real \
file:line / matched-string anchor, correct anything the fact sheet got wrong, and add \
what it missed. Then re-commit your refined findings with submit_findings — that \
refined submission, not the draft, is your report."""

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

STATIC_TESTER_PROMPT = """\
You are a Tester verifying a coder's change in an environment with NO runnable test \
suite (no pytest, heavy deps like torch absent, grading tests withheld by design). Do \
NOT call run_tests or pytest. You are GIVEN the plan the coder worked to and the \
coder's report below — verify the edit against THOSE; do NOT go re-derive the spec by \
exploring the codebase. Do EXACTLY these two things, then STOP and emit your verdict:

A. STATIC CHECKS on the edited file(s):
   - grep the edited source ONCE for `raise NotImplementedError` and once for \
`# TODO: implement this function` — if the target body is still a stub, FAIL.
   - run `python3 -m py_compile <edited_file>` once per edited file — any SyntaxError \
is a FAIL (report file:line).
   - signature: confirm the function name and parameters match the plan/goal; a \
renamed/added/dropped param is a FAIL.
B. PLAN CONSISTENCY: read the edited function ONCE and check it does what the PLAN's \
approach and definition of done describe — every branch / behavior the plan names is \
present and not contradicted. A missing or contradicted plan item is a FAIL (name it).

That is ALL. Do NOT open unrelated files, do NOT re-grep a pattern you already ran, do \
NOT run toy tests, do NOT re-derive the spec from scratch. The moment A and B resolve, \
call structured_output: PASS when A is clean AND B is consistent; FAIL with SPECIFIC, \
ACTIONABLE findings (which file, what is wrong or missing) so the coder can fix it. \
Reserve BLOCKED for genuine infra breakage unrelated to the code — NEVER for \
"pytest/torch missing", which is expected here. Set `tests_run` to [] and \
`failed_count` to 0.

{rules}

Goal:
{goal}
{target_tests}

The plan the coder implemented (verify the edit against THIS — do not re-derive it):
- root cause: {root_cause}
- approach: {approach}

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

COMMIT_PROMPT = """\
You are a Coder. You have analyzed this phase but the working tree is still \
unchanged — no edit has landed. STOP investigating and implement the fix NOW: \
based on the confirmed root cause and approach, make the single most likely \
correct edit with file_write or apply_patch this turn. A concrete attempt is far \
better than more analysis; you can refine it after the tester runs. Then report \
in <=5 lines.

{rules}

Goal:
{goal}

Confirmed root cause:
{root_cause}

Approach:
{approach}

Progress so far:
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


# Enforced-mode prompt suffixes (Phase 1, 1D). Appended ONLY when enforcement is
# on — the base PLAN_PROMPT/CODER_PROMPT/FORCED_PROMPT/COMMIT_PROMPT strings are
# never edited, so the OFF prompt text stays byte-for-byte identical to reference.
PLANNER_ENFORCED_SUFFIX = (
    "\n\nYou have ONLY file_read + grep (no shell, no file editing). Confirm facts "
    "by reading/grepping; never run commands or write files."
)
CODER_ENFORCED_SUFFIX = (
    "\n\nYou have NO shell/bash and CANNOT create new files. Edit ONLY the existing "
    "target via file_write str_replace (preferred) or apply_patch. Do NOT attempt "
    "to run python/tests via shell or write helper/test scripts."
)


def _enforcement_on(enforcement_strength: str) -> bool:
    """True when the structural toolset whitelist (Phase 1) is engaged.

    Mirrors ``SessionRun._enforcement_on`` so the workflow can decide locally
    whether to swap the toolset and append the enforced prompt suffix. ``off`` ->
    False -> reference behavior.
    """
    return enforcement_strength != ENFORCEMENT_OFF


def _planner_suffix(enforcement_strength: str) -> str:
    return PLANNER_ENFORCED_SUFFIX if _enforcement_on(enforcement_strength) else ""


def _coder_suffix(enforcement_strength: str) -> str:
    return CODER_ENFORCED_SUFFIX if _enforcement_on(enforcement_strength) else ""


def _final_verify_redundant(
    enforcement_strength: str, forced: bool, phase_reports: list[dict[str, Any]]
) -> bool:
    """STEP 2B (Phase 2): is the whole-goal final tester redundant with the per-phase
    testers that already ran?

    The per-phase coder->tester loop already runs an adversarial tester after each
    phase (``tester:p{idx}r{round}``); a phase only reaches ``status == "passed"``
    once that tester PASSED on the cumulative tree AND cleared the FAIL_TO_PASS gate.
    When EVERY phase passed and NO coder edit has touched the tree since (no forced
    write — the only coder call between the last phase tester and the final verify),
    the ``tester:final`` call would re-run near-identical static checks on a
    byte-identical tree — pure waste (observed in 6/6 traces). Skip it then.

    Conservative by construction: returns False (run the final tester, keeping the
    repair loop intact) whenever any phase failed/blocked, a forced write landed an
    un-reviewed patch, or there are no phase reports. Gated on enforcement, so with
    enforcement OFF this always returns False and the verify path is byte-for-byte
    the reference."""
    if not _enforcement_on(enforcement_strength):
        return False
    if forced or not phase_reports:
        return False
    return all(r.get("status") == "passed" for r in phase_reports)


def _planner_tools(enforcement_strength: str = ENFORCEMENT_OFF) -> list[Any]:
    """Tools for the planning analyst (the PLAN call).

    OFF == reference: returns the exact ``_read_tools()`` list. ON drops bash so
    the planner is confined to read-only file_read + grep — it cannot run shell
    commands or overwrite source via a ``cat >`` redirect (the CRITICAL
    planner-overwrite vector).
    """
    if enforcement_strength == ENFORCEMENT_OFF:
        return _read_tools()
    return [FileReadTool(), GrepTool()]


def _coder_tools(enforcement_strength: str = ENFORCEMENT_OFF) -> list[Any]:
    """Tools for the implement/forced/repair coder calls.

    OFF == reference: returns the exact current 6-tool list AND order. ON drops
    bash (no shell test-theater / find / helper-script creation) and restricts
    file_write to str_replace only (``allow_create=False``), keeping the coder's
    habitual edit path plus apply_patch + run_tests + read/grep.
    """
    if enforcement_strength == ENFORCEMENT_OFF:
        return [BashTool(), FileReadTool(), FileWriteTool(), ApplyPatchTool(), RunTestsTool(), GrepTool()]
    return [FileReadTool(), GrepTool(), FileWriteTool(allow_create=False), ApplyPatchTool(), RunTestsTool()]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), RunTestsTool(), GrepTool()]


def _static_tester_tools() -> list[Any]:
    # No RunTestsTool: where no test runtime exists, run_tests can only waste
    # budget. Static validation needs bash (py_compile), file_read, grep.
    return [BashTool(), FileReadTool(), GrepTool()]


def _tester_prompt(static_verify: bool) -> str:
    return STATIC_TESTER_PROMPT if static_verify else TESTER_PROMPT


def _tester_tools_for(static_verify: bool) -> list[Any]:
    return _static_tester_tools() if static_verify else _tester_tools()


def _time_low(ctx: Any) -> bool:
    """True when the run is within the deadline margin (wall-clock-aware).

    Defensive: a ctx without ``time_low`` (unbounded CLI runs, older test stubs)
    reports False so behavior is unchanged where no deadline is wired.
    """
    time_low = getattr(ctx, "time_low", None)
    return bool(time_low()) if callable(time_low) else False


def _budget_ok(ctx: Any, reserve: int = RESERVE_TOKENS) -> bool:
    """True while there is BOTH enough token budget AND enough wall-clock time
    left for another full coder/tester step.

    ``reserve`` is the headroom that must remain AFTER this step. The implement
    loop uses the default ``RESERVE_TOKENS`` so it always leaves room for a
    forced write plus a final verify; the wrap-up verify/repair pass it ``0`` so
    it runs on whatever the reserve preserved (a forced write capped at
    ``FORCED_WRITE_BUDGET`` cannot eat it all). Bails early once
    ``ctx.time_low()`` reports the hard deadline is near so the reserve is spent
    BEFORE the wall truncates the run (P7 / django-11564 — the edit was located
    but never written because forced-write only checked tokens).
    """
    return ctx.budget.remaining() > reserve and not _time_low(ctx)


async def _recon(
    ctx: Any,
    goal: str,
    dims: list[dict[str, Any]],
    enforcement_strength: str = "off",
    commit_reserve: int = 25_000,
) -> str:
    """Fan the dimensions out to parallel read-only scouts; return a combined,
    labelled findings document for the planning analyst.

    ``enforcement_strength`` (default ``off``) threads the STEP-0 wind-down to each
    scout: with ``off`` the scout runs exactly as before; with ``needs-enforcement``
    it gets a submit_findings tool and the structural commit brake (forced to a
    single structured submit at ~80% of its cap instead of being chopped)."""
    if len(dims) > MAX_SCOUTS:
        await ctx.log(
            f"recon: scope produced {len(dims)} dimensions — capping to {MAX_SCOUTS} scouts"
        )
        dims = dims[:MAX_SCOUTS]

    # STEP 5a/5c (gated on enforcement). OFF -> ``fact_hint`` stays "" and
    # ``depth_leash`` stays 1.0, so the scout count, per-scout cap and hints below
    # are byte-for-byte identical to the reference path.
    fact_hint = ""
    depth_leash = 1.0
    if enforcement_strength != "off":
        # 5a — deterministic, NON-LLM pre-recon fact sheet over the in-workspace
        # (stubbed) source ONLY. Degrade gracefully: a missing workspace root or an
        # un-locatable target yields no manifest, the scouts keep today's hints, and
        # 5c sizing is skipped. The extractor itself refuses to read any answer
        # artifact (test_code/, func_implementation, *_result/_output.jsonl).
        workspace_root = getattr(ctx, "workspace_root", None)
        manifest = None
        if workspace_root:
            try:
                manifest = build_fact_sheet(workspace_root, goal)
            except Exception as exc:  # noqa: BLE001 — recon must never abort on the fact sheet
                await ctx.log(f"recon: fact sheet skipped (extractor error: {exc})")
                manifest = None
        else:
            await ctx.log("recon: fact sheet skipped — no workspace_root on ctx")
        if manifest:
            fact_hint = format_fact_sheet_hint(manifest)
            await ctx.log(
                f"recon: fact sheet built for {manifest['function_name']} "
                f"({manifest['target_file']}): {manifest['call_site_count']} call site(s), "
                f"{len(manifest['siblings'])} sibling(s), "
                f"{len(manifest['referenced_types'])} type ref(s)"
            )
            # 5c — size the scout COUNT + per-scout depth leash from a cheap static
            # complexity estimate, so a trivial target does not get the full fan-out.
            complexity = estimate_target_complexity(manifest)
            n_scouts, depth_leash = size_recon(len(dims), complexity, ceiling=MAX_SCOUTS)
            if n_scouts < len(dims):
                await ctx.log(
                    f"recon: complexity={complexity} -> sizing {len(dims)} dimension(s) "
                    f"down to {n_scouts} scout(s) (depth leash {depth_leash:.2f})"
                )
                dims = dims[:n_scouts]
            else:
                await ctx.log(
                    f"recon: complexity={complexity} -> keeping {len(dims)} scout(s) "
                    f"(depth leash {depth_leash:.2f})"
                )
        elif workspace_root:
            await ctx.log(
                "recon: fact sheet unavailable (goal names no target / file not found) "
                "— scouts use scope hints unchanged, complexity sizing skipped"
            )

    # Deduct recon from a reserved tail: scouts share only (remaining -
    # RECON_FLOOR), so plan/implement/verify always keep RECON_FLOOR no matter how
    # greedily the read-only scouts explore. min() keeps the SCOUT_BUDGET ceiling
    # binding when the pool is large (e.g. a 2M run).
    n = len(dims)
    recon_pool = max(0, ctx.budget.remaining() - RECON_FLOOR)
    scout_cap = min(SCOUT_BUDGET, recon_pool // n) if n else SCOUT_BUDGET
    # 5c depth leash: shrink each scout's cap for simpler targets so a lone scout
    # cannot just absorb the budget freed by dropping its peers. ``1.0`` (OFF, or
    # the complex bucket) leaves scout_cap untouched.
    if depth_leash < 1.0:
        scout_cap = max(1, int(scout_cap * depth_leash))
    await ctx.log(
        f"recon: {n} scout(s), {ctx.budget.remaining() // 1000}k remaining, "
        f"holding {RECON_FLOOR // 1000}k for plan/implement/verify → "
        f"scout cap {scout_cap // 1000}k each"
    )
    if enforcement_strength != "off":
        # Reserve is carved FROM each scout's cap (explore_threshold =
        # scout_cap - reserve_size), never additive — log it so the wind-down
        # trip point is auditable against submit_turn_cost in the metric.
        await ctx.log(
            f"recon: enforcement={enforcement_strength}, reserve_size={commit_reserve} "
            f"(explore_threshold ~{max(0, scout_cap - commit_reserve) // 1000}k of "
            f"{scout_cap // 1000}k per scout)"
        )

    def _scout_label(d: dict[str, Any], i: int) -> str:
        return f"scout:{i}:{(d.get('aspect') or '').strip().replace(' ', '-')[:24] or 'dim'}"

    def _scout_hints(d: dict[str, Any]) -> str:
        base = "\n".join(d.get("hints") or []) or "(no starting point given — search from the goal)"
        # 5a injection: prepend the static fact sheet so scouts start from confirmed
        # signatures/call-sites instead of re-discovering them. Empty when OFF or no
        # manifest -> returns ``base`` byte-for-byte.
        return f"{fact_hint}\n\n{base}" if fact_hint else base

    # STEP 5b — commit-first (Design B, no FSM changes). Gated on enforcement AND a
    # built fact sheet (``fact_hint`` non-empty) AND a ctx that can run a bounded
    # submit-only draft call. For each scout, commit a turn-0 DRAFT from the static
    # fact sheet (one bounded ``draft_findings`` call) BEFORE it explores; the scout
    # then runs EXACTLY as today (capture→cancel→harvest unchanged), revising the
    # draft into its own refined submit (which is what gets harvested). The draft is
    # also passed as the per-scout HARVEST FALLBACK so a scout that dies/strays before
    # refining never loses the fact-sheet anchors. OFF / no manifest / no draft_findings
    # -> ``draft_texts`` stays all-None and every scout call is byte-for-byte reference.
    draft_texts: list[str | None] = [None] * len(dims)
    draft_fn = getattr(ctx, "draft_findings", None)
    if fact_hint and callable(draft_fn):
        await ctx.log(f"recon: commit-first — drafting {len(dims)} scout(s) from the fact sheet")
        draft_payloads = await ctx.parallel(
            [
                (
                    lambda d=d, i=i: draft_fn(
                        DRAFT_PROMPT.format(
                            aspect=d.get("aspect", f"dimension {i}"),
                            question=d.get("question", ""),
                            fact_hint=fact_hint,
                            hints="\n".join(d.get("hints") or [])
                            or "(no starting point given — search from the goal)",
                        ),
                        label=f"{_scout_label(d, i)}:draft",
                        budget=commit_reserve,
                    )
                )
                for i, d in enumerate(dims)
            ]
        )
        for i, payload in enumerate(draft_payloads):
            if isinstance(payload, dict):
                rendered = format_findings_report(payload)
                if rendered.strip():
                    draft_texts[i] = rendered
        drafted = sum(1 for t in draft_texts if t)
        await ctx.log(f"recon: commit-first — {drafted}/{len(dims)} draft(s) committed")

    def _draft_block(i: int) -> str:
        return DRAFT_REVISE_BLOCK.format(draft=draft_texts[i]) if draft_texts[i] else ""

    reports = await ctx.parallel(
        [
            (
                lambda d=d, i=i: ctx.agent(
                    SCOUT_PROMPT.format(
                        rules=SHARED_RULES,
                        goal=goal,
                        aspect=d.get("aspect", f"dimension {i}"),
                        question=d.get("question", ""),
                        hints=_scout_hints(d),
                        draft_block=_draft_block(i),
                    ),
                    label=_scout_label(d, i),
                    tools=_read_tools(),
                    budget=scout_cap,
                    enforcement_strength=enforcement_strength,
                    commit_reserve=commit_reserve,
                    harvest_fallback=draft_texts[i],
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
    injected_test_paths: list[str] | None = None,
    static_verify: bool = False,
    enforcement_strength: str = ENFORCEMENT_OFF,
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
    # Source-scope the working-tree gates: the SWE-bench harness ``git apply``s
    # the FAIL_TO_PASS test_patch WITHOUT committing, so the tree is dirty the
    # whole run. Excluding those injected paths makes the gates fire on the
    # AGENT's edit, not the harness's. Empty (CLI / non-SWE-bench) -> behaves as
    # ``tree_changed`` byte-for-byte.
    _inj = injected_test_paths or []
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
            )
            + _coder_suffix(enforcement_strength),
            label=f"coder:p{idx}r{round_no}",
            tools=_coder_tools(enforcement_strength),
            budget=CODER_BUDGET,
        )
        # Rung C — early commit (django-11564 step-235 failure mode): a coder that
        # ends having landed NO edit at all this phase (tree still clean) analyzed
        # without committing. Don't spend a tester round verifying nothing —
        # re-issue ONCE with the commit-now forced prompt and a forced tool call,
        # then verify that. Budget-gated (the round top already checked) and
        # bounded to once per round; complements the session-level read-without-
        # write escalation (which can't fire once a coder turn has already
        # stop-ped) and the budget-floor forced write (still the last resort).
        if (await ctx.source_changed(_inj)) is False:
            await ctx.log(
                f"phase {idx} round {round_no}: coder landed no edit — forcing a commit before testing"
            )
            forced_summary = await ctx.agent(
                COMMIT_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    root_cause=root_cause,
                    approach=approach,
                    progress=f"Round {round_no} coder analyzed but wrote nothing; commit the fix now.",
                )
                + _coder_suffix(enforcement_strength),
                label=f"coder:p{idx}r{round_no}-commit",
                tools=_coder_tools(enforcement_strength),
                tool_choice="required",
                budget=CODER_BUDGET,
            )
            if forced_summary is not None:
                summary = forced_summary
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
            _tester_prompt(static_verify).format(
                rules=SHARED_RULES,
                goal=phase_goal,
                done=done,
                target_tests=target_tests,
                summary=coder_summary,
                root_cause=root_cause,
                approach=approach,
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:p{idx}r{round_no}",
            tools=_tester_tools_for(static_verify),
            budget=TESTER_BUDGET,
        )
        # Diff guard: a tester PASS must NOT stand if the working tree is
        # verifiably unchanged this round — no edit means nothing to pass. Seed
        # the next round so the coder is told it MUST write; on the final round
        # signal the run to force a write.
        tree = await ctx.source_changed(_inj)
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
        if verdict is None:
            await ctx.log(
                f"phase {idx} round {round_no} tester subagent DIED "
                "(no verdict — agent error/timeout/budget) — substituting generic findings"
            )
        elif not isinstance(verdict, dict):
            await ctx.log(
                f"phase {idx} round {round_no} tester returned an UNEXPECTED type "
                f"({type(verdict).__name__}) — substituting generic findings"
            )
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
    ctx: Any,
    goal: str,
    root_cause: str,
    approach: str,
    progress: str,
    *,
    reason: str,
    injected_test_paths: list[str] | None = None,
    enforcement_strength: str = ENFORCEMENT_OFF,
) -> str:
    """Spend the reserved headroom on one coder that MUST land an edit.

    ``reason`` distinguishes the trigger in the log ("budget low" vs "empty tree
    after implement"). The coder runs with ``tool_choice="required"`` so the
    provider forces a tool call — the session layer falls back to "auto" once if
    the endpoint rejects "required".

    This is the last action of the run and its whole job is to GUARANTEE a patch
    lands before the hard wall, so it is hardened three ways:
    ``over_budget_ok=True`` skips ``WorkflowContext.agent``'s pre-call budget raise
    so the write still runs after the meter hits zero — without it the forced write
    self-aborted on an exhausted budget and no coder round ran at all (sympy-11400);
    ``thinking=False`` forces reasoning off so the generation is fast and cannot
    blow the deadline margin even when the run-wide default is thinking-on
    (analyst-solve eval runs with OPENCOLLAB_THINKING=1); and ``timeout`` clamps
    the call to whatever wall-clock time is left, so a stalled call is cancelled
    inside the workflow — its on-disk edits survive — instead of being truncated
    by the outer wall (which lost django-11564).
    """
    await ctx.log(f"forced write: {reason} — landing a best-effort patch")
    result = await ctx.agent(
        FORCED_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            root_cause=root_cause,
            approach=approach,
            progress=progress or "(no prior coder edits recorded)",
        )
        + _coder_suffix(enforcement_strength),
        label="coder:forced-write",
        tools=_coder_tools(enforcement_strength),
        tool_choice="required",
        thinking=False,
        timeout=_seconds_left(ctx),
        over_budget_ok=True,
        budget=FORCED_WRITE_BUDGET,
    )
    # Post-attempt outcome so the trajectory distinguishes a patch that LANDED from
    # one that ABORTED (coder died / timed out / budget). Prefer the SOURCE probe —
    # ground truth that an edit reached disk OUTSIDE the harness-injected tests —
    # and fall back to the coder's return value when no probe is wired (CLI / older
    # stubs report None).
    probe = getattr(ctx, "source_changed", None)
    changed = await probe(injected_test_paths or []) if callable(probe) else None
    if changed is True:
        await ctx.log(f"forced write: {reason} — LANDED a patch (working tree changed)")
    elif changed is False:
        await ctx.log(f"forced write: {reason} — ABORTED: no edit reached disk")
    elif result is not None:
        await ctx.log(f"forced write: {reason} — coder returned (tree change unverified)")
    else:
        await ctx.log(f"forced write: {reason} — ABORTED: coder died before writing")
    return result


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
    # No in-loop test runtime (KOCO) -> tester validates statically instead of
    # running tests. General flag; default False keeps SWE-bench/CLI identical.
    static_verify = bool(args.get("static_verify"))
    # Enforcement wind-down (STEP 0). Default ``off`` keeps every run byte-for-byte
    # identical; ``needs-enforcement`` arms the structural commit brake for
    # budget-myopic models so a read-only scout commits a structured submit at ~80%
    # of its cap instead of being chopped mid-exploration.
    enforcement_strength = str(args.get("enforcement_strength") or "off")
    commit_reserve = int(args.get("commit_reserve") or 25_000)
    # Paths the harness ``git apply``ed (FAIL_TO_PASS test files) but did NOT
    # commit — the tree is dirty with them the whole run. The working-tree gates
    # exclude these so they fire on the agent's SOURCE edit, not the harness's
    # injected tests. Empty for CLI / non-SWE-bench runs (gates == tree_changed).
    injected_test_paths = list(args.get("injected_test_paths") or [])

    # Phase 1 — analyst frames the investigation.
    await ctx.phase("scope")
    scope = await ctx.agent(
        SCOPE_PROMPT.format(rules=SHARED_RULES, goal=goal, target_tests=target_tests),
        schema=DIMENSIONS_SCHEMA,
        label="analyst:scope",
        tools=_read_tools(),
        budget=SCOPE_BUDGET,
    )
    dims = scope.get("dimensions") if isinstance(scope, dict) else None

    # Phase 2 — parallel reconnaissance (skipped gracefully if framing failed).
    await ctx.phase("recon")
    if dims:
        findings_doc = await _recon(ctx, goal, dims, enforcement_strength, commit_reserve)
    else:
        await ctx.log("recon skipped — analyst produced no dimensions")
        findings_doc = "(reconnaissance skipped — proceed from the goal itself)"

    # Phase 3 — analyst designs the phased plan from the findings.
    await ctx.phase("plan")
    plan = await ctx.agent(
        PLAN_PROMPT.format(
            rules=SHARED_RULES, goal=goal, target_tests=target_tests, findings=findings_doc
        )
        + _planner_suffix(enforcement_strength),
        schema=PLAN_SCHEMA,
        label="analyst:plan",
        tools=_planner_tools(enforcement_strength),
        budget=PLAN_BUDGET,
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
            ctx, goal, root_cause, approach, ph, idx, target_tests, fail_to_pass,
            injected_test_paths, static_verify, enforcement_strength,
        )
        phase_reports.append(report)
        if report["status"] in ("budget_low", "empty_tree"):
            progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
            reason = "budget low" if report["status"] == "budget_low" else "empty tree after phase"
            await _forced_final_write(
                ctx, goal, root_cause, approach, progress, reason=reason,
                injected_test_paths=injected_test_paths,
                enforcement_strength=enforcement_strength,
            )
            forced = True
            break
        # Best-effort: a failed/blocked phase does NOT stop the run.
        await ctx.log(f"phase {idx} {report['status']} after {report.get('rounds', 0)} round(s)")

    # P0-2 — forced write on an empty tree, independent of budget. Even when no
    # phase signalled budget_low/empty_tree, if every phase finished but the
    # working tree is still verifiably empty, land a best-effort patch before the
    # final verify rather than reporting "done" with no edit. ``None`` (no probe
    # wired) is treated as "cannot verify" and does NOT trigger a forced write.
    if not forced and (await ctx.source_changed(injected_test_paths)) is False:
        progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
        await _forced_final_write(
            ctx, goal, root_cause, approach, progress, reason="empty tree after implement",
            injected_test_paths=injected_test_paths,
            enforcement_strength=enforcement_strength,
        )
        forced = True

    # Phase 5 — one whole-goal verification, with a single repair round if affordable.
    await ctx.phase("verify")
    final_verdict: dict[str, Any] | None = None
    repaired = False
    # STEP 2B (Phase 2): skip the whole-goal final tester when every phase already
    # passed its own adversarial tester on the current tree and no forced write has
    # touched it since — re-running it would be near-identical checks on the same
    # tree (pure waste). Enforcement-gated, so OFF runs the final tester exactly as
    # the reference. The repair loop stays intact for any failed/blocked phase.
    skip_final_verify = _final_verify_redundant(enforcement_strength, forced, phase_reports)
    if skip_final_verify:
        await ctx.log(
            "verify: skipping redundant final tester — all phases passed and no "
            "intervening coder edit (enforcement on)"
        )
    # Run verify EVEN AFTER a forced write: the forced-write coder lands an
    # un-reviewed patch on budget-low, so verifying (and repairing) it is exactly
    # when it matters most. The implement loop reserved RESERVE_TOKENS for this
    # wrap-up and FORCED_WRITE_BUDGET caps the forced write, so a verify slice
    # always survives — hence the light ``reserve=0`` gate (any budget + time).
    if _budget_ok(ctx, 0) and not skip_final_verify:
        final_verdict = await ctx.agent(
            _tester_prompt(static_verify).format(
                rules=SHARED_RULES,
                goal=goal,
                done=FINAL_DONE,
                target_tests=target_tests,
                summary="\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports)),
                root_cause=root_cause,
                approach=approach,
            ),
            schema=VERDICT_SCHEMA,
            label="tester:final",
            tools=_tester_tools_for(static_verify),
            budget=TESTER_BUDGET,
        )
        if (
            isinstance(final_verdict, dict)
            and final_verdict.get("verdict") == "FAIL"
            and _budget_ok(ctx, 0)
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
                )
                + _coder_suffix(enforcement_strength),
                label="coder:repair",
                tools=_coder_tools(enforcement_strength),
                budget=REPAIR_BUDGET,
            )
            final_verdict = await ctx.agent(
                _tester_prompt(static_verify).format(
                    rules=SHARED_RULES,
                    goal=goal,
                    done=FINAL_DONE,
                    target_tests=target_tests,
                    summary="(post-repair re-check)",
                    root_cause=root_cause,
                    approach=approach,
                ),
                schema=VERDICT_SCHEMA,
                label="tester:final2",
                tools=_tester_tools_for(static_verify),
                budget=TESTER_BUDGET,
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

    # A run cannot be "done" unless the working tree actually changed in SOURCE
    # (excluding harness-injected tests). The probe answers True/False when wired,
    # or None when it cannot verify. On None we keep the self-reported outcome but
    # flag it as unverified so the caller knows the success was not corroborated by
    # a real diff.
    tree = await ctx.source_changed(injected_test_paths)
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
        # Key name retained for back-compat; its meaning is now SOURCE-scoped
        # (changes outside injected_test_paths), not whole-tree. No external consumer.
        "tree_changed": tree,
        "tokens_spent": ctx.budget.spent(),
    }
    if tree is None:
        result["tree_unverified"] = True
    # STEP 2B: surface the skip for trace auditing. Added ONLY when it fired, so the
    # off-path (and the non-skipped on-path) result shape is unchanged.
    if skip_final_verify:
        result["final_verify_skipped"] = True
    return result
