"""Duo retains complete evidence in files for read-only adjudication."""

from __future__ import annotations

import json
from typing import Any

from opencollab.patches import patch_paths
from opencollab.workflows import CandidateRun

from . import _selection as contract
from ._candidate_evidence_files import CandidateEvidenceFiles, ReadCandidateEvidence
from ._dual_coder import _review_and_select
from ._prompts import SELECTION_PROMPT
from ._rules import SHARED_RULES

_INLINE_EVIDENCE_MAX_BYTES = 128_000

FILE_EVIDENCE_INSTRUCTIONS = """
The candidate evidence above is a directory of complete saved files. Use
read_candidate_evidence to read each candidate's index and public evidence,
then read the result reports and diff ranges needed to assess the task requirements.
When inline_comparison is present, it contains both exact candidate diffs and
shared public test records for direct comparison. The saved files remain available.
Each index entry identifies original changed paths, a diff file, and character
offset and length. Large binary patches remain fully available in those files.
Every read returns next_offset and eof; continue reading whenever needed.
Evidence paths are read through the tool, even when the candidate environment
is separate from the workflow host. No shell access or candidate modifications are allowed.
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
    rules: str = SHARED_RULES,
) -> tuple[str, Any, str]:
    """Compare complete small diffs directly and retain paged evidence for all sizes."""
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
    comparison, _, _ = contract._judge_input(candidate_a, candidate_b)
    if len(comparison.encode("utf-8")) <= _INLINE_EVIDENCE_MAX_BYTES:
        evidence["inline_comparison"] = json.loads(comparison)
    prompt = selector_prompt.format(
        rules=rules, goal=goal,
        candidates=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
    ) + FILE_EVIDENCE_INSTRUCTIONS
    return await _review_and_select(ctx, prompt=prompt, paths=paths, tools=[ReadCandidateEvidence(files)])
