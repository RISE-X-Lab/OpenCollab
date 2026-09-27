"""Duo, OpenCollab's built-in task workflow, and evaluator integration helpers."""

from __future__ import annotations

from ._dual_coder import run_dual_coder
from ._prompts import CONTRACT_PROMPT
from .duo import duo


def get_builtin_workflows():
    """Return a fresh registry exposing the single installed Duo workflow."""
    from opencollab.application.workflow_registry import Registry

    registry = Registry()
    registry.register(duo.__workflow_spec__)
    return registry


__all__ = ["duo", "get_builtin_workflows", "run_dual_coder", "CONTRACT_PROMPT"]
