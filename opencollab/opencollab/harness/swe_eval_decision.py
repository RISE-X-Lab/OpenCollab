"""Pure state decisions for SWE-bench generation and evaluation runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from opencollab.harness.swe_eval_records import (
    metric_done_with_advisory_gap,
    metric_status,
    prediction_patch,
    row_patch_sha,
    row_record_id,
)


class TaskState(StrEnum):
    NEEDS_GENERATION = "needs_generation"
    GENERATION_ACTIVE = "generation_active"
    EMPTY_PATCH_INVALID = "empty_patch_invalid"
    BLOCKED_MISSING_METRIC = "blocked_missing_metric"
    BLOCKED_METRIC_PAIRING = "blocked_metric_pairing"
    WORKFLOW_INCOMPLETE = "workflow_incomplete"
    WORKFLOW_FAILED = "workflow_failed"
    READY_FOR_EVAL = "ready_for_eval"
    EVAL_ACTIVE = "eval_active"
    EVAL_DONE = "eval_done"
    TECHNICAL_EVAL_FAILED = "technical_eval_failed"


TERMINAL_WORKFLOW_STATUSES = {
    "infra_invalid",
    "budget_exceeded",
    "timeout",
    "patch_guard_failed",
}

TECHNICAL_EVAL_STATUSES = {
    "technical_eval_failed",
    "eval_failed",
    "eval_start_failed",
    "eval_driver_error",
    "blocked_missing_eval_deps",
    "blocked_missing_eval_image",
    "blocked_missing_eval_spec",
}


@dataclass(frozen=True)
class EvalReportSummary:
    done_count: int = 0
    active_count: int = 0
    failed_count: int = 0
    resolved_count: int = 0
    unresolved_count: int = 0
    ignored_patch_mismatch_count: int = 0
    report_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "done_count": self.done_count,
            "active_count": self.active_count,
            "failed_count": self.failed_count,
            "resolved_count": self.resolved_count,
            "unresolved_count": self.unresolved_count,
            "ignored_patch_mismatch_count": self.ignored_patch_mismatch_count,
            "report_paths": list(self.report_paths),
        }


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    active_generation: bool = False
    active_eval: bool = False
    prediction: dict[str, Any] | None = None
    metric: dict[str, Any] | None = None
    metric_pairing: str = ""
    eval_summary: EvalReportSummary = field(default_factory=EvalReportSummary)


@dataclass(frozen=True)
class TaskDecision:
    task_id: str
    state: TaskState
    ready_for_eval: bool
    terminal: bool
    patch_len: int
    patch_sha256: str
    workflow_status: str
    metric_pairing: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "state": self.state.value,
            "ready_for_eval": self.ready_for_eval,
            "terminal": self.terminal,
            "patch_len": self.patch_len,
            "patch_sha256": self.patch_sha256,
            "workflow_status": self.workflow_status,
            "metric_pairing": self.metric_pairing,
            "reason": self.reason,
        }


def official_eval_eligible(
    *,
    patch_len: int,
    workflow_status: str,
    active_generation: bool,
    metric: dict[str, Any] | None,
    allow_advisory_gap: bool = False,
) -> bool:
    if active_generation or patch_len <= 0:
        return False
    if workflow_status in {"done", "done_with_timeout_patch"} and not metric_done_with_advisory_gap(metric):
        return True
    if allow_advisory_gap and workflow_status in {"advisory_gap", "done"}:
        return True
    return False


def decide_task(snapshot: TaskSnapshot, *, allow_advisory_gap: bool = False) -> TaskDecision:
    patch = prediction_patch(snapshot.prediction)
    patch_len = len(patch)
    patch_sha = row_patch_sha(snapshot.prediction)
    status = metric_status(snapshot.metric)

    def decision(state: TaskState, reason: str, *, ready: bool = False, terminal: bool = False) -> TaskDecision:
        return TaskDecision(
            task_id=snapshot.task_id,
            state=state,
            ready_for_eval=ready,
            terminal=terminal,
            patch_len=patch_len,
            patch_sha256=patch_sha,
            workflow_status=status,
            metric_pairing=snapshot.metric_pairing,
            reason=reason,
        )

    if snapshot.active_generation:
        return decision(TaskState.GENERATION_ACTIVE, "generation session is active")
    if snapshot.prediction is None:
        return decision(TaskState.NEEDS_GENERATION, "no prediction row")
    if patch_len <= 0:
        return decision(TaskState.EMPTY_PATCH_INVALID, "latest prediction has an empty patch", terminal=True)
    if snapshot.metric is None:
        if snapshot.metric_pairing.startswith("record_id_patch_sha"):
            return decision(TaskState.BLOCKED_METRIC_PAIRING, snapshot.metric_pairing, terminal=True)
        return decision(TaskState.BLOCKED_MISSING_METRIC, snapshot.metric_pairing or "missing metric")
    if snapshot.active_eval or snapshot.eval_summary.active_count:
        return decision(TaskState.EVAL_ACTIVE, "evaluation session is active")
    if snapshot.eval_summary.done_count:
        return decision(TaskState.EVAL_DONE, "matching evaluation report is done", terminal=True)
    if snapshot.eval_summary.failed_count:
        return decision(TaskState.TECHNICAL_EVAL_FAILED, "matching evaluation report failed", terminal=True)

    ready = official_eval_eligible(
        patch_len=patch_len,
        workflow_status=status,
        active_generation=snapshot.active_generation,
        metric=snapshot.metric,
        allow_advisory_gap=allow_advisory_gap,
    )
    if ready:
        return decision(TaskState.READY_FOR_EVAL, "non-empty patch with terminal workflow metric", ready=True)
    if status in TERMINAL_WORKFLOW_STATUSES:
        return decision(TaskState.WORKFLOW_FAILED, status, terminal=True)
    return decision(TaskState.WORKFLOW_INCOMPLETE, status or "workflow metric is not terminal")


def task_status_row(snapshot: TaskSnapshot, *, allow_advisory_gap: bool = False) -> dict[str, Any]:
    decision = decide_task(snapshot, allow_advisory_gap=allow_advisory_gap)
    row = decision.to_dict()
    checkpoint_result = {}
    if isinstance(snapshot.metric, dict) and isinstance(snapshot.metric.get("checkpoint_result"), dict):
        checkpoint_result = snapshot.metric["checkpoint_result"]
    row.update(
        {
            "record_id": row_record_id(snapshot.prediction),
            "eval": snapshot.eval_summary.to_dict(),
            "checkpoint_result": checkpoint_result,
        }
    )
    return row
