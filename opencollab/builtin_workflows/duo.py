"""Duo solves a task through two isolated candidates and evidence-based selection."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import workflow

from ._dual_coder import run_dual_coder
from ._file_selection import adjudicate_candidate_files
from ._prompts import _PROMPT_REVISION, SELECTION_PROMPT


@workflow(
    name="duo",
    description="Two focused solutions with complete evidence and read-only selection",
    phases=["candidate-a", "candidate-b", "mechanical-selection", "adjudication", "adoption"],
)
async def duo(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run Duo using task-oriented prompts and complete candidate evidence.

    ``candidate_evidence_dir`` optionally supplies a host-side parent directory.
    Each adjudication creates its own retained evidence directory.
    ``submission_mode="working_tree"`` declares caller-owned patch capture and
    later submission. The default ``"task"`` follows task-specific delivery.
    """
    async def adjudicator(context: Any, **options: Any) -> tuple[str, Any, str]:
        return await adjudicate_candidate_files(
            context, **options, evidence_parent=args.get("candidate_evidence_dir"),
        )

    result = await run_dual_coder(
        ctx, args, selector_prompt=SELECTION_PROMPT, adjudicator=adjudicator,
    )
    result["prompt_revision"] = _PROMPT_REVISION
    return result


__all__ = ["duo"]
