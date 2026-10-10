"""OpenCollab's built-in task workflows and integration helpers."""

from __future__ import annotations

from ._dual_coder import run_dual_coder
from ._prompts import CONTRACT_PROMPT
from .duo import duo
from .evolution import (
    EvolutionAdapter,
    EvolutionCheck,
    EvolutionConfig,
    EvolutionGroup,
    EvolutionState,
    evolution,
    plan_evolution_groups,
    run_evolution,
)


def get_builtin_workflows():
    """Return a fresh registry exposing the installed task workflows."""
    from opencollab.application.workflow_registry import Registry

    registry = Registry()
    registry.register(duo.__workflow_spec__)
    registry.register(evolution.__workflow_spec__)
    return registry


__all__ = [
    "duo", "evolution", "get_builtin_workflows", "run_dual_coder", "CONTRACT_PROMPT",
    "EvolutionAdapter", "EvolutionCheck", "EvolutionConfig", "EvolutionGroup", "EvolutionState",
    "plan_evolution_groups", "run_evolution",
]
