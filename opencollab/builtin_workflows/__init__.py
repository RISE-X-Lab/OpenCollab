"""OpenCollab's built-in task workflows and integration helpers."""

from __future__ import annotations

from ._dual_coder import run_dual_coder
from ._prompts import CONTRACT_PROMPT
from .duo import duo
from .weave import (
    WeaveAdapter,
    WeaveCheck,
    WeaveConfig,
    WeaveGroup,
    WeaveState,
    plan_weave_groups,
    run_weave,
    weave,
)


def get_builtin_workflows():
    """Return a fresh registry exposing the installed task workflows."""
    from opencollab.application.workflow_registry import Registry

    registry = Registry()
    registry.register(duo.__workflow_spec__)
    registry.register(weave.__workflow_spec__)
    return registry


__all__ = [
    "duo", "weave", "get_builtin_workflows", "run_dual_coder", "CONTRACT_PROMPT",
    "WeaveAdapter", "WeaveCheck", "WeaveConfig", "WeaveGroup", "WeaveState",
    "plan_weave_groups", "run_weave",
]
