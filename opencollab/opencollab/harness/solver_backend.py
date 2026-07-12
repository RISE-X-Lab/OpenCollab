"""Solver backend protocol for SWE evaluation runs."""

from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from opencollab.harness.eval_adapter.models import (
    PatchCandidate,
    PreparedWorkspace,
    TaskSpec,
)

_FORBIDDEN_SOLVER_METADATA_KEYS = frozenset(
    {
        "basecommit",
        "beforereposetcmd",
        "dockerhubtag",
        "dockerimage",
        "failtopass",
        "goldpatch",
        "instanceid",
        "passtopass",
        "referencepatch",
        "selectedtestfiles",
        "servicedependencies",
        "testpatch",
    }
)
_MODEL_RUNTIME_OPTIONS = (
    ("--model-name", "OPENCOLLAB_SWE_MODEL_NAME"),
    ("--llm-model", "OPENCOLLAB_SWE_LLM_MODEL"),
)


@dataclass(frozen=True, slots=True)
class SolverTaskView:
    """Public task information that a solver may inspect."""

    task_id: str
    repo: str
    problem_statement: str
    hints: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hints", tuple(str(value) for value in self.hints))
        public_metadata = deepcopy(dict(self.metadata))
        _reject_hidden_metadata(public_metadata)
        object.__setattr__(self, "metadata", MappingProxyType(public_metadata))


def solver_task_view(task: TaskSpec) -> SolverTaskView:
    """Create a fail-closed public view from normalized benchmark metadata."""

    metadata = task.metadata
    identity = f"{task.repo}\0{task.problem_statement}".encode("utf-8")
    public_task_id = "solver-" + hashlib.sha256(identity).hexdigest()[:32]
    raw_hints = metadata.get("solver_public_hints")
    hints = (
        tuple(str(value) for value in raw_hints)
        if isinstance(raw_hints, (list, tuple))
        else ()
    )
    raw_public_metadata = metadata.get("solver_public_metadata")
    public_metadata = (
        dict(raw_public_metadata) if isinstance(raw_public_metadata, Mapping) else {}
    )
    _reject_hidden_values(task, (public_task_id, *hints, public_metadata))
    return SolverTaskView(
        task_id=public_task_id,
        repo=task.repo,
        problem_statement=task.problem_statement,
        hints=hints,
        metadata=public_metadata,
    )


def _reject_hidden_metadata(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("solver public metadata keys must be strings")
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _FORBIDDEN_SOLVER_METADATA_KEYS or normalized == "patch":
                raise ValueError(f"solver public metadata contains hidden field: {key}")
            _reject_hidden_metadata(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_hidden_metadata(item)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("solver public metadata must contain only JSON-like values")


def _iter_strings(value: Any) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            values.extend(_iter_strings(str(key)))
            values.extend(_iter_strings(item))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            values.extend(_iter_strings(item))
    return tuple(values)


def _hidden_task_strings(task: TaskSpec) -> tuple[str, ...]:
    explicit_hidden = (
        task.instance_id,
        task.base_commit,
        task.docker_image,
        task.dockerhub_tag,
        task.fail_to_pass,
        task.pass_to_pass,
        task.selected_test_files,
        task.test_patch,
        task.reference_patch,
        task.before_repo_set_cmd,
        task.service_dependencies,
    )
    private_metadata = {
        key: value
        for key, value in task.metadata.items()
        if re.sub(r"[^a-z0-9]", "", str(key).lower())
        not in {
            "solverpublichints",
            "solverpublicmetadata",
            "solverpublictaskid",
        }
    }
    public_values = {
        value.casefold()
        for value in (task.repo.strip(), task.problem_statement.strip(), task.dataset.strip())
        if value
    }
    return tuple(
        value
        for value in _iter_strings((explicit_hidden, private_metadata))
        if value.strip() and value.strip().casefold() not in public_values
    )


def _reject_hidden_values(task: TaskSpec, public_value: Any) -> None:
    hidden_values = _hidden_task_strings(task)
    for candidate in _iter_strings(public_value):
        public = candidate.strip().casefold()
        if not public:
            continue
        for hidden_candidate in hidden_values:
            hidden = hidden_candidate.strip().casefold()
            if (
                public == hidden
                or (len(hidden) >= 8 and hidden in public)
                or (len(public) >= 16 and public in hidden)
            ):
                raise ValueError("solver public data contains hidden task information")


@dataclass(frozen=True, slots=True)
class SolverBudget:
    """Generation budget handed from the evaluation layer to a solver."""

    max_tokens: int | None = None
    timeout_seconds: int | None = None
    max_attempts: int = 1


@runtime_checkable
class SolverBackend(Protocol):
    """A cooperation strategy that turns one task workspace into one patch."""

    name: str

    def solve(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        ...


class SolverContractError(RuntimeError):
    """A solver returned a candidate that cannot be attributed safely."""


def solve_with_public_task(
    backend: SolverBackend,
    task: TaskSpec,
    workspace: PreparedWorkspace,
    run_dir: Path,
    budget: SolverBudget,
) -> PatchCandidate:
    """Invoke a solver through the public task boundary and validate attribution."""

    public_task = solver_task_view(task)
    backend_name = getattr(backend, "name", None)
    if not isinstance(backend_name, str) or not backend_name.strip():
        raise SolverContractError("solver backend name must be a non-empty string")
    candidate = backend.solve(
        public_task,
        workspace,
        run_dir,
        budget,
    )
    _validate_candidate(
        backend_name=backend_name,
        task=public_task,
        candidate=candidate,
    )
    return replace(candidate, task_id=task.instance_id)


def _validate_candidate(
    *,
    backend_name: str,
    task: SolverTaskView,
    candidate: Any,
) -> None:
    if not isinstance(candidate, PatchCandidate):
        raise SolverContractError("solver must return a PatchCandidate")
    if candidate.task_id != task.task_id:
        raise SolverContractError(
            "solver candidate task_id does not match the public task_id"
        )
    if candidate.solver_name != backend_name:
        raise SolverContractError(
            "solver candidate solver_name does not match the backend name"
        )
    if not isinstance(candidate.patch, str):
        raise SolverContractError("solver candidate patch must be a string")
    if not isinstance(candidate.metadata, Mapping):
        raise SolverContractError("solver candidate metadata must be a mapping")
    for field_name in ("record_id", "log_path"):
        if not isinstance(getattr(candidate, field_name), str):
            raise SolverContractError(
                f"solver candidate {field_name} must be a string"
            )
    if (
        isinstance(candidate.token_count, bool)
        or not isinstance(candidate.token_count, int)
        or candidate.token_count < 0
    ):
        raise SolverContractError(
            "solver candidate token_count must be a non-negative integer"
        )
    if (
        isinstance(candidate.cost_usd, bool)
        or not isinstance(candidate.cost_usd, (int, float))
        or not math.isfinite(float(candidate.cost_usd))
        or candidate.cost_usd < 0
    ):
        raise SolverContractError(
            "solver candidate cost_usd must be a finite non-negative number"
        )


@dataclass(frozen=True, slots=True)
class WorkflowSolverSpec:
    """Configuration for a backend implemented as an OpenCollab workflow."""

    name: str
    workflow_name: str
    description: str
    max_attempts: int = 1
    default_budget_tokens: int | None = None
    required_runtime_options: tuple[tuple[str, str], ...] = ()
    config_overrides: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)


DEFAULT_WORKFLOW_SOLVERS: dict[str, WorkflowSolverSpec] = {
    "g11": WorkflowSolverSpec(
        name="g11",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "g1.1": WorkflowSolverSpec(
        name="g1.1",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "baseTeam": WorkflowSolverSpec(
        name="baseTeam",
        workflow_name="base-team",
        description="Analyst, coder, and tester as a deterministic workflow.",
        max_attempts=1,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "TeamPro": WorkflowSolverSpec(
        name="TeamPro",
        workflow_name="team-pro",
        description="Dynamic analyst-led reconnaissance and phased coder/tester workflow.",
        max_attempts=3,
        default_budget_tokens=4_000_000,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
        config_overrides={
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
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
        config_overrides={
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
    "SolverContractError",
    "SolverTaskView",
    "WorkflowSolverSpec",
    "solve_with_public_task",
    "solver_task_view",
    "workflow_solver_spec",
]
