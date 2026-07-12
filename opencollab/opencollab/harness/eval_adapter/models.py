"""Typed records shared by SWE evaluation adapters and solver backends."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencollab.harness.swe_eval_records import patch_sha


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Benchmark task metadata normalized before a solver sees the task."""

    instance_id: str
    dataset: str
    repo: str
    problem_statement: str
    base_commit: str = ""
    docker_image: str = ""
    dockerhub_tag: str = ""
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    selected_test_files: tuple[str, ...] = ()
    test_patch: str = ""
    reference_patch: str = ""
    before_repo_set_cmd: str = ""
    service_dependencies: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fail_to_pass", tuple(self.fail_to_pass))
        object.__setattr__(self, "pass_to_pass", tuple(self.pass_to_pass))
        object.__setattr__(self, "selected_test_files", tuple(self.selected_test_files))
        object.__setattr__(self, "service_dependencies", tuple(self.service_dependencies))


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """Container/workspace requirements for a normalized task."""

    image: str
    repo_root_candidates: tuple[str, ...] = ("/app", "/testbed")
    service_dependencies: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root_candidates", tuple(self.repo_root_candidates))
        object.__setattr__(self, "service_dependencies", tuple(self.service_dependencies))


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """A running workspace handed to a solver backend."""

    container_id: str
    repo_root: str
    workdir: str
    env: Mapping[str, str] = field(default_factory=dict)
    cleanup: Callable[[], None] | None = None

    def close(self) -> None:
        if self.cleanup is not None:
            self.cleanup()


@dataclass(frozen=True, slots=True)
class PatchCandidate:
    """Patch and generation accounting produced by a solver backend."""

    task_id: str
    solver_name: str
    patch: str
    record_id: str = ""
    log_path: str = ""
    token_count: int = 0
    cost_usd: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def patch_sha256(self) -> str:
        return patch_sha(self.patch)

    @property
    def is_empty(self) -> bool:
        return not self.patch.strip()


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Official evaluation result for one patch candidate."""

    task_id: str
    patch_sha256: str
    eval_done: bool
    resolved: bool
    technical_failed: bool = False
    technical_reasons: tuple[str, ...] = ()
    report_path: str = ""
    log_path: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "technical_reasons", tuple(self.technical_reasons))


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Joined view of task, candidate, official eval, and accounting."""

    task: TaskSpec
    solver_name: str
    run_dir: Path
    attempt: int
    candidate: PatchCandidate | None = None
    eval_result: EvalResult | None = None

    @property
    def task_id(self) -> str:
        return self.task.instance_id

    @property
    def final_patch_sha256(self) -> str:
        if self.candidate is None:
            return ""
        return self.candidate.patch_sha256
