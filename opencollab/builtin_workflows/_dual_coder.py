"""Duo role execution and adoption using OpenCollab's candidate runtime."""

from __future__ import annotations

from typing import Any

from opencollab.patches import patch_paths
from opencollab.tools import evidence_tools
from opencollab.workflows import CandidateRun

from . import _candidate_records as dual
from . import _selection as contract
from ._rules import SHARED_RULES, _complete_goal, coder_role_timeout_seconds, structured_role_timeout_seconds

MINIMAL_CODER_PROMPT = """\
You are autonomous coder A. Work only in this isolated candidate worktree.

{rules}

Public issue
{goal}

Find the narrowest root cause that fully explains the issue. Preserve backward
compatibility and existing public behavior outside the requested change. Trace
the immediate callers and consumers needed to verify the fix, then implement a
minimal complete source patch. Avoid broad refactors, speculative cleanup, test
edits, generated files, caches, and logs. Run the nearest relevant public tests
with bash, inspect the final diff, and finish with a non-empty source diff.
Do not use official results, hidden tests, FAIL_TO_PASS ids, grader patches, or
historical outcomes."""


CROSS_COMPONENT_CODER_PROMPT = """\
You are autonomous coder B. Work only in this isolated candidate worktree.

{rules}

Public issue
{goal}

Shared public command observed from candidate A
{public_command}

Solve the issue end to end with emphasis on cross-component completeness.
Trace every producer and producing state or data path, every direct consumer, public API and
serialization contract, error propagation, lifecycle boundary, and relevant
edge cases. Implement the smallest patch that covers the whole contract while
preserving unrelated behavior. Avoid test edits, generated files, caches, and
logs. Run relevant public tests through Bash using the project's native test command. When the shared command is
available, execute the same native command without replacing
it with an easier test. Inspect the final diff and finish with a non-empty source
patch. Do not use official results, hidden tests, FAIL_TO_PASS ids, grader
patches, or historical outcomes."""


CONTRACT_PROMPT = """\
You are the read-only contract adjudicator for two autonomous coder candidates.
Candidate A was instructed to make the narrowest compatible root-cause repair.
Candidate B was instructed to cover producer, consumer, API, lifecycle, and
edge-case contracts. You cannot edit, merge, or rerun either candidate.

{rules}

Public issue
{goal}

Candidate evidence
{candidates}

Enumerate every explicit behavior requirement in the public issue. For each
requirement, compare the actual A and B diffs against the relevant producer,
consumer, and public API behavior. Cite concrete changed paths and diff details.
Public test records are comparable only when target, runner, and command are
identical. Do not reward larger diffs, stylistic changes, or unsupported claims.
Choose B only when public evidence shows B covers at least one requirement A
does not cover and no requirement is better covered by A. Set
requirements_complete true only after accounting for every explicit issue
requirement. Do not use official outcomes, hidden tests, FAIL_TO_PASS ids,
grader patches, historical results, or model identity."""


def _coder_tools(*, allow_unisolated_shell: bool = False) -> list[Any]:
    return list(evidence_tools(
        "bash", "file_read", "file_write", "apply_patch", "grep", "git_diff",
        headless=not allow_unisolated_shell,
    ))


async def _coder_candidate(
    ctx: Any,
    *,
    label: str,
    prompt: str,
    goal: str,
    shared_command: str = "",
    allow_unisolated_shell: bool = False,
) -> CandidateRun:
    tools = _coder_tools(allow_unisolated_shell=allow_unisolated_shell)
    raw = await ctx.candidate_agent(
        prompt.format(
            rules=SHARED_RULES,
            goal=goal,
            public_command=shared_command or "(no shared public command)",
        ),
        label=label,
        tools=tools,
        budget=None,
        timeout=coder_role_timeout_seconds(),
    )
    records = [
        dual._public_record(record)
        for record in raw.test_records
        if isinstance(record, dict)
    ]
    observed_command = str(records[0].get("command") or "").strip() if records else shared_command
    return CandidateRun(
        label=raw.label,
        output={
            "coder_output": raw.output,
            "public_command": observed_command,
            "public_test_records": records,
        },
        diff=raw.diff,
        test_records=raw.test_records,
        verified_targets=raw.verified_targets,
    )


async def _contract_adjudicate(
    ctx: Any,
    *,
    goal: str,
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
    selector_prompt: str = CONTRACT_PROMPT,
) -> tuple[str, Any, str]:
    evidence, paths, truncated = contract._judge_input(candidate_a, candidate_b)
    if truncated:
        return "A", None, "contract-evidence-incomplete-default-a"
    try:
        result = await ctx.agent(
            selector_prompt.format(
                rules=SHARED_RULES,
                goal=goal,
                candidates=evidence,
            ),
            schema=contract.CONTRACT_SCHEMA,
            label="dual-coder-contract-adjudicator",
            tools=[],
            budget=None,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"dual coder contract adjudicator unavailable after {type(exc).__name__}")
        result = None
    winner = contract._validated_judge_winner(result, paths)
    if winner is None:
        return "A", result, "contract-evidence-insufficient-default-a"
    return winner, result, "contract-adjudicated"


async def run_dual_coder(
    ctx: Any,
    args: dict[str, Any],
    *,
    selector_prompt: str = CONTRACT_PROMPT,
    adjudicator: Any = None,
) -> dict[str, Any]:
    """Run the existing candidate and selection sequence with a per-call prompt."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    allow_unisolated_shell = args.get("allow_unisolated_shell", False)
    if not isinstance(allow_unisolated_shell, bool):
        raise ValueError("allow_unisolated_shell must be a boolean")

    source_before = await ctx.diff()

    await ctx.phase("dual-coder-a")
    candidate_a = await _coder_candidate(
        ctx,
        label="dual-coder-contract-a",
        prompt=MINIMAL_CODER_PROMPT,
        goal=goal,
        allow_unisolated_shell=allow_unisolated_shell,
    )
    shared_command = dual._candidate_command(candidate_a)

    await ctx.phase("dual-coder-b")
    candidate_b = await _coder_candidate(
        ctx,
        label="dual-coder-contract-b",
        prompt=CROSS_COMPONENT_CODER_PROMPT,
        goal=goal,
        shared_command=shared_command,
        allow_unisolated_shell=allow_unisolated_shell,
    )

    await ctx.phase("dual-coder-mechanical-selection")
    winner, reason, needs_judge = contract._mechanical_choice(
        candidate_a,
        candidate_b,
    )
    judge_result: Any = None
    if needs_judge:
        await ctx.phase("dual-coder-contract-adjudication")
        choose = adjudicator if adjudicator is not None else _contract_adjudicate
        winner, judge_result, reason = await choose(
            ctx,
            goal=goal,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            selector_prompt=selector_prompt,
        )

    source_before_adoption = await ctx.diff()
    if source_before_adoption != source_before:
        raise RuntimeError("candidate_workspace_tracking_failure: source worktree changed before candidate adoption")

    candidates = {"A": candidate_a, "B": candidate_b}
    preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
    adopted, adoption_attempts = await dual._adopt(
        ctx,
        winner=winner,
        candidates=candidates,
        preserve_paths=preserve_paths,
    )
    return {
        "status": "done" if adopted is not None else "incomplete",
        "winner": winner,
        "selection_reason": reason,
        "judge_used": needs_judge,
        "judge_result": judge_result,
        "adopted": adopted,
        "adoption_attempts": adoption_attempts,
        "shared_public_command": shared_command,
        "candidates": {
            "A": {
                "kind": "minimal-root-cause-coder",
                "nonempty": bool(candidate_a.diff.strip()),
                "diff_bytes": len(candidate_a.diff.encode("utf-8")),
                "changed_paths": patch_paths(candidate_a.diff),
                "public_command": dual._candidate_command(candidate_a),
                "public_test_records": dual._candidate_records(candidate_a),
            },
            "B": {
                "kind": "cross-component-contract-coder",
                "nonempty": bool(candidate_b.diff.strip()),
                "diff_bytes": len(candidate_b.diff.encode("utf-8")),
                "changed_paths": patch_paths(candidate_b.diff),
                "public_command": dual._candidate_command(candidate_b),
                "public_test_records": dual._candidate_records(candidate_b),
            },
        },
        "tokens_spent": ctx.tokens_spent(),
    }
