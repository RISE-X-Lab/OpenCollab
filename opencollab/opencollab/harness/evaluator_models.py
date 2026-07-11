"""Public data models used by the headless evaluator facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EvalResult:
    """Result of a single evaluation task."""

    task_id: str
    patch: str
    patch_produced: bool
    tokens_used: int
    steps: int
    duration: float
    error: str | None = None
    trajectory_path: str | None = None
    markup_recovered: int = 0
    workflow_result: Any | None = None
    checkpoint_result: Any | None = None
    test_patch_isolation_failed: bool = False
    execution_quiesced: bool = True
    patch_extraction_succeeded: bool = True
    injected_path_cleanup_proven: bool = True
    harness_artifact_exclusion_proven: bool = True
    checkpoint_restore_integrity_proven: bool = True
    task_stage_integrity_proven: bool = True
    submission_eligible: bool = True


@dataclass
class EvalTask:
    """A single evaluation task, such as one SWE-bench instance."""

    task_id: str
    description: str
    repo_path: str | None = None
    docker_image: str | None = None
    timeout: float = 600.0
    max_tokens: int = 1_000_000
    extras: dict | None = None
    harness_artifact_paths: tuple[str, ...] = ()


__all__ = ["EvalResult", "EvalTask"]
