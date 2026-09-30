"""Duo retains complete evidence in files for read-only adjudication."""

from __future__ import annotations

import json
from typing import Any

from opencollab.patches import patch_paths
from opencollab.workflows import CandidateRun

from . import _candidate_records as records
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
Each index entry identifies original changed paths, a diff file, and character
offset and length. Large binary patches remain fully available in those files.
Every read returns next_offset and eof; continue reading whenever needed.
Evidence paths are read through the tool, even when the candidate environment
is separate from the workflow host. No shell access or candidate modifications are allowed.
Cite original changed paths and inspected diff details in a_evidence and
b_evidence, not the evidence storage paths. File existence or a claimed test
success alone does not establish that a requirement is covered.
"""

_INLINE_EVIDENCE_INSTRUCTIONS = """
The inline_comparison above contains both complete candidate diffs, all individual
public test records, shared comparable records, and the model-supplied reports.
Assess this evidence directly. The index paths identify retained originals for
the caller. Produce the structured decision using the complete supplied content.
Treat model-supplied reports as claims and execution records according to their
verified status. Cite original changed paths and concrete behavior in each entry.
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
    comparison_text, _, _ = contract._judge_input(candidate_a, candidate_b)
    comparison = json.loads(comparison_text)
    for label, candidate in (("A", candidate_a), ("B", candidate_b)):
        comparison[label].update(
            public_test_records=records._candidate_records(candidate),
            candidate_report=(records._candidate_output(candidate).get("coder_output")
                              if isinstance(candidate.output, dict) else candidate.output),
            report_is_model_supplied=True,
        )
    comparison_size = len(json.dumps(comparison, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if comparison_size <= _INLINE_EVIDENCE_MAX_BYTES:
        evidence["inline_comparison"] = comparison
        instructions = _INLINE_EVIDENCE_INSTRUCTIONS
        tools: list[Any] = []
    else:
        instructions = FILE_EVIDENCE_INSTRUCTIONS
        tools = [ReadCandidateEvidence(files)]
    prompt = selector_prompt.format(
        rules=rules, goal=goal,
        candidates=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
    ) + instructions
    return await _review_and_select(ctx, prompt=prompt, paths=paths, tools=tools)
