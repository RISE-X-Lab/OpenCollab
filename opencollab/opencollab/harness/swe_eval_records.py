"""Small, pure helpers for SWE-bench prediction/metric records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PairedRows:
    prediction: dict[str, Any] | None
    metric: dict[str, Any] | None
    status: str


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def prediction_patch(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("model_patch") or row.get("patch") or "")


def row_task_id(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("instance_id", "task_id", "id"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def row_record_id(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("record_id", "attempt_id", "workflow_record_id"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def row_explicit_patch_sha(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def patch_sha(patch: str) -> str:
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def row_patch_sha(row: dict[str, Any] | None) -> str:
    explicit = row_explicit_patch_sha(row)
    if explicit:
        return explicit
    return patch_sha(prediction_patch(row))


def patch_sha_matches(left: str | None, right: str | None) -> bool:
    left_value = str(left or "")
    right_value = str(right or "")
    if not left_value or not right_value:
        return False
    return left_value.startswith(right_value) or right_value.startswith(left_value)


def workflow_result(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    value = row.get("workflow_result")
    return value if isinstance(value, dict) else {}


def metric_status(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    result = workflow_result(row)
    return str(row.get("workflow_status") or result.get("status") or "")


def metric_done_with_advisory_gap(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    result = workflow_result(row)
    return bool(row.get("done_with_advisory_gap") or result.get("done_with_advisory_gap"))


def task_ids(predictions: list[dict[str, Any]], metrics: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in [*predictions, *metrics]:
        task_id = row_task_id(row)
        if task_id and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered


def latest_paired_rows(
    predictions: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    task_id: str,
) -> PairedRows:
    matched_predictions = [row for row in predictions if row_task_id(row) == task_id]
    matched_metrics = [row for row in metrics if row_task_id(row) == task_id]
    if not matched_predictions:
        return PairedRows(None, matched_metrics[-1] if matched_metrics else None, "missing_prediction")

    prediction = matched_predictions[-1]
    record_id = row_record_id(prediction)
    current_sha = row_patch_sha(prediction)

    if record_id:
        record_metrics = [row for row in matched_metrics if row_record_id(row) == record_id]
        if record_metrics:
            metric = record_metrics[-1]
            metric_sha = row_patch_sha(metric)
            if current_sha and metric_sha and not patch_sha_matches(metric_sha, current_sha):
                return PairedRows(prediction, None, "record_id_patch_sha_mismatch")
            if current_sha and not metric_sha:
                return PairedRows(prediction, None, "record_id_patch_sha_missing")
            return PairedRows(prediction, metric, "record_id")
        return PairedRows(prediction, None, "missing_metric_for_record_id")

    if current_sha:
        for metric in reversed(matched_metrics):
            metric_sha = row_patch_sha(metric)
            if metric_sha and patch_sha_matches(metric_sha, current_sha):
                return PairedRows(prediction, metric, "patch_sha")
        if row_explicit_patch_sha(prediction):
            return PairedRows(prediction, None, "missing_metric_for_patch_sha")

    legacy_metrics = [row for row in matched_metrics if not row_record_id(row) and not row_explicit_patch_sha(row)]
    if legacy_metrics:
        return PairedRows(prediction, legacy_metrics[-1], "legacy_latest")
    return PairedRows(prediction, None, "missing_metric")
