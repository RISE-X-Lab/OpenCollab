"""Duo solves a public task with two isolated coder strategies."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import workflow

from . import _dual_coder as dual_coder

SELECTION_PROMPT = dual_coder.CONTRACT_PROMPT + """

Read the public requirements and interface declarations item by item before
choosing a candidate. Check call argument positions and types, return values,
error types, and any explicitly required wording against each actual diff.
Separate successful compilation, candidate-written self-checks, and execution
of the relevant public target behavior. Check a self-check's expected values
against the public requirement before treating its success as evidence.

For every requirement entry, put the exact candidate changed file path and
supporting diff details in that entry's own a_evidence and b_evidence arrays.
A path in another entry or in the overall rationale does not support this
entry. For a claimed winner advantage, cite the exact changed path in the
winner's evidence and explain the behavior it adds or preserves. Verify that
the advantage leaves all other requirements intact. Use unclear when concrete
evidence is unavailable. Never invent coverage or an advantage.
"""


@workflow(
    name="duo",
    description="Duo dual coder with explicit public-requirement selector evidence",
    phases=["minimal-coder", "cross-component-coder", "mechanical-selection", "contract-adjudication", "adoption"],
)
async def duo(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Apply the Duo selector prompt to the unchanged dual-coder sequence."""
    return await dual_coder.run_dual_coder(
        ctx,
        args,
        selector_prompt=SELECTION_PROMPT,
    )


__all__ = ["duo"]
