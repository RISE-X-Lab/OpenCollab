"""Duo v3 retains complete evidence in files for read-only adjudication."""

from __future__ import annotations

import json
from typing import Any

from opencollab.patches import patch_paths
from opencollab.workflows import CandidateRun, workflow

from . import _dual_coder as dual_coder
from . import _selection as contract
from ._candidate_evidence_files import CandidateEvidenceFiles, ReadCandidateEvidence
from ._rules import SHARED_RULES, structured_role_timeout_seconds
from .duo import SELECTION_PROMPT

FILE_EVIDENCE_INSTRUCTIONS = """
The candidate evidence above is a directory of complete saved files. Use
read_candidate_evidence to read each candidate's index and public evidence,
then read the actual diff ranges needed to assess the public requirements.
Each index entry identifies original changed paths, a diff file, and character
offset and length. Large binary patches remain fully available in those files.
Every read returns next_offset and eof; continue reading whenever needed.
Evidence paths are read through the tool, even when the candidate environment
is a separate container. No shell access or candidate modifications are allowed.
Cite original changed paths and inspected diff details in a_evidence and
b_evidence, not the evidence storage paths. File existence or a claimed test
success alone does not establish that a requirement is covered.
"""


async def adjudicate_candidate_files(
    ctx: Any,
    *,
    goal: str,
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
    selector_prompt: str = SELECTION_PROMPT,
    evidence_parent: str | None = None,
) -> tuple[str, Any, str]:
    """Use the original selection rules with complete, paged file evidence."""
    files = CandidateEvidenceFiles(evidence_parent)
    evidence = {
        "A": files.add_candidate("A", candidate_a),
        "B": files.add_candidate("B", candidate_b),
        "shared_public_test_records_path": files.add_shared_records(
            contract._shared_public_records(candidate_a, candidate_b)
        ),
    }
    await ctx.log(f"Duo complete adjudication evidence directory: {files.directory}")
    paths = {"A": patch_paths(candidate_a.diff), "B": patch_paths(candidate_b.diff)}
    try:
        result = await ctx.agent(
            selector_prompt.format(
                rules=SHARED_RULES, goal=goal,
                candidates=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            ) + FILE_EVIDENCE_INSTRUCTIONS,
            schema=contract.CONTRACT_SCHEMA,
            label="dual-coder-contract-adjudicator",
            tools=[ReadCandidateEvidence(files)],
            budget=None,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"dual coder file adjudicator unavailable after {type(exc).__name__}")
        result = None
    winner = contract._validated_judge_winner(result, paths)
    if winner is None:
        return "A", result, "contract-evidence-insufficient-default-a"
    return winner, result, "contract-adjudicated"


@workflow(
    name="duo-v3",
    description="Duo dual coder with complete file evidence and read-only selector tools",
    phases=["minimal-coder", "cross-component-coder", "mechanical-selection", "contract-adjudication", "adoption"],
)
async def duo_v3(
    ctx: Any, args: dict[str, Any],
) -> dict[str, Any]:
    """Keep A/B generation and adoption unchanged; use files for the judge.

    ``candidate_evidence_dir`` optionally selects a host-side parent directory
    for evidence retention. Each adjudication creates an independent child.
    The directory is retained and its location is recorded in workflow logs.
    """
    async def adjudicator(context: Any, **options: Any) -> tuple[str, Any, str]:
        return await adjudicate_candidate_files(
            context, **options, evidence_parent=args.get("candidate_evidence_dir"),
        )

    return await dual_coder.run_dual_coder(
        ctx, args, selector_prompt=SELECTION_PROMPT, adjudicator=adjudicator,
    )


__all__ = ["duo_v3"]
