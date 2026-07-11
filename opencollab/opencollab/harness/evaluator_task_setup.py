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
    task_id = facade._validate_task_id(task.task_id)
    if not isinstance(task.description, str):
        raise ValueError("task description must be a string")
    if isinstance(task.max_tokens, bool):
        raise ValueError("task max_tokens must be a positive integer")
    try:
        task_max_tokens = operator.index(task.max_tokens)
    except TypeError as exc:
        raise ValueError("task max_tokens must be a positive integer") from exc
    if task_max_tokens <= 0:
        raise ValueError("task max_tokens must be a positive integer")
    if isinstance(max_steps, bool):
        raise ValueError("max_steps must be a positive integer")
    try:
        normalized_max_steps = operator.index(max_steps)
    except TypeError as exc:
        raise ValueError("max_steps must be a positive integer") from exc
    if normalized_max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if task.extras is not None and not isinstance(task.extras, dict):
        raise ValueError("task extras must be a dictionary or None")
    if isinstance(task.extras, dict) and "test_patch" in task.extras and not isinstance(task.extras["test_patch"], str):
        raise ValueError("task extras test_patch must be a string")
    harness_artifact_inputs = facade._validate_harness_artifact_paths(task.harness_artifact_paths)
    try:
        task_timeout = float(task.timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("task timeout must be a finite positive number") from exc
    if isinstance(task.timeout, bool) or not math.isfinite(task_timeout) or task_timeout <= 0:
        raise ValueError("task timeout must be a finite positive number")

    checkpoint_interval: float | None = None
    if checkpoint_interval_seconds is not None:
        try:
            checkpoint_interval = float(checkpoint_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("checkpoint_interval_seconds must be finite and non-negative") from exc
        if (
            isinstance(checkpoint_interval_seconds, bool)
            or not math.isfinite(checkpoint_interval)
            or checkpoint_interval < 0
        ):
            raise ValueError("checkpoint_interval_seconds must be finite and non-negative")
        if checkpoint_interval == 0:
            checkpoint_interval = None

    try:
        cleanup_timeout = float(cancellation_cleanup_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("cancellation_cleanup_timeout must be a finite positive number") from exc
    if isinstance(cancellation_cleanup_timeout, bool) or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
        raise ValueError("cancellation_cleanup_timeout must be a finite positive number")

    normalized_task = replace(
        task,
        task_id=task_id,
        timeout=task_timeout,
        max_tokens=task_max_tokens,
        extras=dict(task.extras) if task.extras is not None else None,
        harness_artifact_paths=harness_artifact_inputs,
    )
    start = time.monotonic()
    facade.ensure_directory_no_symlinks(output_dir)
    trajectories_dir = os.path.join(output_dir, "trajectories")
    run_dir: str | None = None
    if workflow is None:
        tracer = facade.Tracer(run_id=task_id, output_dir=trajectories_dir)
    else:
        run_dir = os.path.join(trajectories_dir, task_id)
        tracer = facade.Tracer(
            run_id=task_id,
            output_dir=run_dir,
            filename=facade.ORCHESTRATION_FILENAME,
        )
    return PreparedEvalRun(
        task=normalized_task,
        max_steps=normalized_max_steps,
        checkpoint_interval=checkpoint_interval,
        cleanup_timeout=cleanup_timeout,
        start=start,
        task_deadline=start + task_timeout,
        trajectories_dir=trajectories_dir,
        run_dir=run_dir,
        tracer=tracer,
    )
