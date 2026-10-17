"""The reconstruction workflow: probe, hypothesise, code, check, repeat.

One agent does the whole loop; the workflow only adds a bounded repair pass
when the final candidate still disagrees with recorded oracle answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .tasks import Task, jsonable, outputs_match
from .tools import CandidateRunner, QueryLog

CANDIDATE_FILE = "candidate.py"

TASK_PROMPT = """\
You are reverse-engineering a hidden program from its input/output behaviour only.
You cannot see its code. You can only feed it inputs with `query_oracle` and
observe the outputs.

Input type: {input_type}. {constraints}

Deliverable: write `{candidate}` in the workspace defining
`{function_name}(input_data)` that returns exactly what the hidden program
returns for ALL valid inputs, not only the ones you queried. Use only the
Python standard library (no numpy or other third-party packages).

Method:
1. Probe with a few diverse inputs first, including edge cases (empty list,
   single element, zeros, negatives, duplicates, long inputs).
2. Form the simplest hypothesis consistent with all evidence. Prefer constants,
   then arithmetic, then conditionals, then loops; never hard-code observed cases.
3. Write the candidate file, then run `check_candidate` on it.
4. Query NEW inputs chosen to distinguish your hypothesis from plausible
   alternatives, re-check, and refine.
5. Finish only when `check_candidate` reports every recorded case matches and
   your distinguishing inputs also agree.

Oracle budget: {max_queries} queries in total. Spend them deliberately.
When done, reply with one paragraph stating the rule you found.
"""

FIXED_PROMPT = """\
You are reverse-engineering a hidden program from its input/output behaviour only.
You cannot see its code and you cannot run it. The examples below are ALL the
evidence you will ever get.

Input type: {input_type}. {constraints}

Examples:
{examples}

Deliverable: write `{candidate}` in the workspace defining
`{function_name}(input_data)` that returns exactly what the hidden program
returns for ALL valid inputs, not only these examples. Use only the Python
standard library (no numpy or other third-party packages).

Method:
1. Form the simplest hypothesis consistent with all examples. Prefer constants,
   then arithmetic, then conditionals, then loops; never hard-code the examples.
2. Write the candidate file, then run `check_candidate` on it.
3. If it fails, revise the hypothesis; finish only when every example matches
   and the rule is general.

When done, reply with one paragraph stating the rule you found.
"""

REPAIR_PROMPT = """\
Your `{candidate}` still disagrees with the hidden program on these recorded
oracle answers:

{failures}

Fix `{candidate}` so it matches every recorded case while staying general.
Run `check_candidate` again before finishing, and reply with the rule you found.
"""


async def _failures(task: Task, log: QueryLog, program: Path, runner: CandidateRunner) -> list[str]:
    cases = [e for e in log.entries if "output" in e]
    if not cases:
        return ["(no oracle answers recorded)"]
    results = await runner.run(program, task.function_name, [c["input"] for c in cases])
    out = []
    for case, res in zip(cases, results):
        if not res["ok"]:
            out.append(f"{case['input']!r}: expected {case['output']!r}, got {res['error']}")
        elif not outputs_match(res["output"], jsonable(case["output"])):
            out.append(f"{case['input']!r}: expected {case['output']!r}, got {res['output']!r}")
    return out


def make_workflow(
    task: Task,
    tools: list[Any],
    log: QueryLog,
    workspace: Path,
    runner: CandidateRunner,
    *,
    max_queries: int,
    max_rounds: int,
    mode: str = "active",
):
    """Build the workflow function OpenCollab will run for one task.

    ``mode`` is "active" (the agent queries the oracle itself) or "fixed"
    (the paper's main setting: the visible examples are the only evidence).
    """

    async def reconstruct(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        program = workspace / CANDIDATE_FILE
        if mode == "fixed":
            prompt = FIXED_PROMPT.format(
                input_type=task.input_type,
                constraints=task.input_constraints,
                examples="\n".join(f"  {i!r} -> {o!r}" for i, o in task.visible),
                candidate=CANDIDATE_FILE,
                function_name=task.function_name,
            )
        else:
            prompt = TASK_PROMPT.format(
                input_type=task.input_type,
                constraints=task.input_constraints,
                candidate=CANDIDATE_FILE,
                function_name=task.function_name,
                max_queries=max_queries,
            )
        rounds: list[dict[str, Any]] = []
        for round_no in range(1, max_rounds + 1):
            await ctx.phase("probe-and-code" if round_no == 1 else "repair")
            reply = await ctx.agent(prompt, label=f"reconstruct:{round_no}", tools=tools)
            failures = (
                await _failures(task, log, program, runner) if program.is_file() else [f"{CANDIDATE_FILE} missing"]
            )
            rounds.append(
                {
                    "round": round_no,
                    "reply": reply,
                    "queries_so_far": log.count,
                    "recorded_failures": len(failures),
                }
            )
            if not failures:
                break
            prompt = REPAIR_PROMPT.format(candidate=CANDIDATE_FILE, failures="\n".join(failures[:10]))
        return {
            "rounds": rounds,
            "queries": log.count,
            "tokens_spent": ctx.tokens_spent(),
            "candidate_exists": program.is_file(),
        }

    return reconstruct
