"""Solver backend protocol for SWE evaluation runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from opencollab.harness.eval_adapter.models import (
    PatchCandidate,
    PreparedWorkspace,
    TaskSpec,
)


@dataclass(frozen=True, slots=True)
class SolverBudget:
    """Generation budget handed from the evaluation layer to a solver."""

    max_tokens: int | None = None
    timeout_seconds: int | None = None
    max_attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SolverBackend(Protocol):
    """A cooperation strategy that turns one task workspace into one patch."""

    name: str

    def solve(
        self,
        task: TaskSpec,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        ...


@dataclass(frozen=True, slots=True)
class WorkflowSolverSpec:
    """Configuration for a backend implemented as an OpenCollab workflow."""

    name: str
    workflow_name: str
    description: str
    max_attempts: int = 1
    default_budget_tokens: int | None = None
    default_model_name: str | None = None
    config_overrides: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)


DEFAULT_WORKFLOW_SOLVERS: dict[str, WorkflowSolverSpec] = {
    "g11": WorkflowSolverSpec(
        name="g11",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
    ),
    "g1.1": WorkflowSolverSpec(
        name="g1.1",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
    ),
    "baseTeam": WorkflowSolverSpec(
        name="baseTeam",
        workflow_name="base-team",
        description="Analyst, coder, and tester as a deterministic workflow.",
        max_attempts=1,
    ),
    "TeamPro": WorkflowSolverSpec(
        name="TeamPro",
        workflow_name="team-pro",
        description="Dynamic analyst-led reconnaissance and phased coder/tester workflow.",
        max_attempts=3,
        default_budget_tokens=4_000_000,
        default_model_name="opencollab-glm52-teampro-prolite",
        config_overrides={
            "model": "glm-5.2",
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32_768,
        },
    ),
    "openhands": WorkflowSolverSpec(
        name="openhands",
        workflow_name="openhands-external",
        description="External OpenHands solver invoked by a configured command template.",
        max_attempts=2,
        default_budget_tokens=16_000_000,
        default_model_name="openhands-1.16.0-glm-5.2",
        config_overrides={
            "model": "anthropic/glm-5.2",
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32_768,
        },
        args={
            "openhands_command": (
                '"$OPENCOLLAB_REMOTE_REPO/scripts/run_openhands_cli.sh" '
                "--headless --json --override-with-envs --file {prompt_file}"
            ),
            "max_steps": 120,
            "openhands_empty_patch_rejections": 2,
            "max_empty_patch_retries": 1,
            "max_eval_attempts": 2,
        },
    ),
}


def workflow_solver_spec(name: str) -> WorkflowSolverSpec:
    return DEFAULT_WORKFLOW_SOLVERS[name]


__all__ = [
    "DEFAULT_WORKFLOW_SOLVERS",
    "SolverBackend",
    "SolverBudget",
    "WorkflowSolverSpec",
    "workflow_solver_spec",
]
