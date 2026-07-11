"""Input validation and tracer setup for evaluator tasks."""

from __future__ import annotations

import math
import operator
import os
import time
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class PreparedEvalRun:
    task: Any
    max_steps: int
    checkpoint_interval: float | None
    cleanup_timeout: float
    start: float
    task_deadline: float
    trajectories_dir: str
    run_dir: str | None
    tracer: Any


def _positive_integer(value: Any, *, name: str) -> int:
    message = f"{name} must be a positive integer"
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        normalized = operator.index(value)
    except TypeError as exc:
        raise ValueError(message) from exc
    if normalized <= 0:
        raise ValueError(message)
    return normalized


def _positive_finite_number(value: Any, *, message: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if isinstance(value, bool) or not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(message)
    return normalized


def _checkpoint_interval(value: float | None) -> float | None:
    if value is None:
        return None
    message = "checkpoint_interval_seconds must be finite and non-negative"
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if isinstance(value, bool) or not math.isfinite(normalized) or normalized < 0:
        raise ValueError(message)
    return normalized or None


def _validate_task(facade: Any, task: Any, max_steps: int) -> tuple[Any, int]:
    task_id = facade._validate_task_id(task.task_id)
    if not isinstance(task.description, str):
        raise ValueError("task description must be a string")
    task_max_tokens = _positive_integer(task.max_tokens, name="task max_tokens")
    normalized_max_steps = _positive_integer(max_steps, name="max_steps")
    if task.extras is not None and not isinstance(task.extras, dict):
        raise ValueError("task extras must be a dictionary or None")
    if (
        isinstance(task.extras, dict)
        and "test_patch" in task.extras
        and not isinstance(task.extras["test_patch"], str)
    ):
        raise ValueError("task extras test_patch must be a string")
    artifact_paths = facade._validate_harness_artifact_paths(
        task.harness_artifact_paths
    )
    task_timeout = _positive_finite_number(
        task.timeout,
        message="task timeout must be a finite positive number",
    )
    normalized_task = replace(
        task,
        task_id=task_id,
        timeout=task_timeout,
        max_tokens=task_max_tokens,
        extras=dict(task.extras) if task.extras is not None else None,
        harness_artifact_paths=artifact_paths,
    )
    return normalized_task, normalized_max_steps


def _create_tracer(
    facade: Any,
    *,
    task_id: str,
    output_dir: str,
    workflow: Any,
) -> tuple[str, str | None, Any]:
    trajectories_dir = os.path.join(output_dir, "trajectories")
    if workflow is None:
        tracer = facade.Tracer(run_id=task_id, output_dir=trajectories_dir)
        return trajectories_dir, None, tracer
    run_dir = os.path.join(trajectories_dir, task_id)
    tracer = facade.Tracer(
        run_id=task_id,
        output_dir=run_dir,
        filename=facade.ORCHESTRATION_FILENAME,
    )
    return trajectories_dir, run_dir, tracer


def prepare_eval_run(
    facade: Any,
    *,
    task: Any,
    output_dir: str,
    workflow: Any,
    max_steps: int,
    checkpoint_interval_seconds: float | None,
    cancellation_cleanup_timeout: float,
) -> PreparedEvalRun:
    """Validate task inputs and create the run's tracer and deadline state."""
    task, max_steps = _validate_task(facade, task, max_steps)
    checkpoint_interval = _checkpoint_interval(checkpoint_interval_seconds)
    cleanup_timeout = _positive_finite_number(
        cancellation_cleanup_timeout,
        message="cancellation_cleanup_timeout must be a finite positive number",
    )
    start = time.monotonic()
    facade.ensure_directory_no_symlinks(output_dir)
    trajectories_dir, run_dir, tracer = _create_tracer(
        facade,
        task_id=task.task_id,
        output_dir=output_dir,
        workflow=workflow,
    )
    return PreparedEvalRun(
        task=task,
        max_steps=max_steps,
        checkpoint_interval=checkpoint_interval,
        cleanup_timeout=cleanup_timeout,
        start=start,
        task_deadline=start + task.timeout,
        trajectories_dir=trajectories_dir,
        run_dir=run_dir,
        tracer=tracer,
    )
