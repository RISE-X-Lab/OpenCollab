"""Built-in OpenCollab workflows and stable evaluator integration points.

``duo`` is the public-task dual-coder workflow. ``duo_v3`` uses the same
selection rules with paged evidence files for its read-only adjudicator.
Legacy workflow names remain available for existing run configurations.
"""

from __future__ import annotations

from typing import Any

from opencollab.workflows import workflow

from ._dual_coder import CONTRACT_PROMPT, run_dual_coder
from ._file_selection import duo_v3
from .duo import duo


@workflow(
    name="validation-council-dual-coder-selection-v2",
    description="Compatibility name for Duo",
    phases=duo.__workflow_spec__.phases,
)
async def validation_council_dual_coder_selection_v2(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run Duo through its original workflow identifier."""
    return await duo(ctx, args)


@workflow(
    name="validation-council-dual-coder-selection-v3",
    description="Compatibility name for Duo with complete file evidence",
    phases=duo_v3.__workflow_spec__.phases,
)
async def validation_council_dual_coder_selection_v3(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run Duo's file-evidence variant through its original identifier."""
    return await duo_v3(ctx, args)


def get_builtin_workflows():
    """Return a fresh registry containing built-ins and compatibility names."""
    from opencollab.application.workflow_registry import Registry

    registry = Registry()
    for fn in (duo, duo_v3, validation_council_dual_coder_selection_v2, validation_council_dual_coder_selection_v3):
        registry.register(fn.__workflow_spec__)
    return registry


__all__ = [
    "duo", "duo_v3", "get_builtin_workflows", "run_dual_coder", "CONTRACT_PROMPT",
    "validation_council_dual_coder_selection_v2", "validation_council_dual_coder_selection_v3",
]
