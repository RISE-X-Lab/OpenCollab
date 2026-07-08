"""Read-only SWE-bench run discovery for thin status scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollab.harness.swe_eval_decision import (
    TECHNICAL_EVAL_STATUSES,
    EvalReportSummary,
    TaskSnapshot,
)
from opencollab.harness.swe_eval_records import (
    latest_paired_rows,
    patch_sha_matches,
    read_jsonl,
    row_patch_sha,
    row_task_id,
    task_ids,
)


@dataclass(frozen=True)
class EvalReport:
    task_id: str
    patch_sha: str
    status: str
    resolved_count: int = 0
    unresolved_count: int = 0
    path: str = ""


def _status_from_official_payload(task_id: str, payload: dict[str, Any]) -> tuple[str, int, int, str] | None:
    item = payload.get(task_id)
    if not isinstance(item, dict):
        return None
    patch_sha = str(item.get("patch_sha256") or item.get("patch_sha") or item.get("model_patch_sha256") or "")
    status = str(item.get("status") or "")
    if status in TECHNICAL_EVAL_STATUSES or item.get("error") is True:
        return "technical_eval_failed", 0, 0, patch_sha
    if not isinstance(item.get("resolved"), bool):
        return None
    resolved = 1 if item["resolved"] else 0
    unresolved = 0 if item["resolved"] else 1
    return "done", resolved, unresolved, patch_sha


def _status_from_summary_payload(payload: dict[str, Any]) -> tuple[str, int, int]:
    status = str(payload.get("status") or "")
    resolved = payload.get("resolved_instances")
    unresolved = payload.get("unresolved_instances")
    if resolved is None and isinstance(payload.get("resolved_ids"), list):
        resolved = len(payload["resolved_ids"])
    if unresolved is None and isinstance(payload.get("unresolved_ids"), list):
        unresolved = len(payload["unresolved_ids"])
    return status, int(resolved or 0), int(unresolved or 0)


def _report_from_json(path: Path) -> EvalReport | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    task_id = str(payload.get("instance_id") or payload.get("task_id") or "")
    status = ""
    resolved = 0
    unresolved = 0
    patch_sha = ""
    if task_id:
        status, resolved, unresolved = _status_from_summary_payload(payload)
    else:
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            official = _status_from_official_payload(str(key), payload)
            if official is None:
                continue
            task_id = str(key)
            status, resolved, unresolved, patch_sha = official
            break
    if not task_id:
        return None
    patch_sha = patch_sha or str(payload.get("patch_sha256") or payload.get("patch_sha") or payload.get("model_patch_sha256") or "")
    return EvalReport(
        task_id=task_id,
        patch_sha=patch_sha,
        status=status,
        resolved_count=resolved,
        unresolved_count=unresolved,
        path=str(path),
    )


def _report_path_sort_key(path: Path) -> tuple[int, str]:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return mtime_ns, str(path)


def discover_eval_reports(side_dir: Path) -> list[EvalReport]:
    if not side_dir.exists():
        return []
    reports: list[EvalReport] = []
    for path in sorted(side_dir.rglob("*.json"), key=_report_path_sort_key):
        report = _report_from_json(path)
        if report is not None:
            reports.append(report)
    return reports


def summarize_eval_reports(
    reports: list[EvalReport],
    *,
    task_id: str,
    current_patch_sha: str,
    active_eval: bool = False,
) -> EvalReportSummary:
    ignored = 0
    latest: EvalReport | None = None
    for report in reports:
        if report.task_id != task_id:
            continue
        if current_patch_sha:
            if not report.patch_sha or not patch_sha_matches(report.patch_sha, current_patch_sha):
                ignored += 1
                continue
        if report.status == "done" or report.status in TECHNICAL_EVAL_STATUSES:
            latest = report

    done = int(latest is not None and latest.status == "done")
    failed = int(latest is not None and latest.status in TECHNICAL_EVAL_STATUSES)
    paths = [latest.path] if latest is not None and latest.path else []
    return EvalReportSummary(
        done_count=done,
        active_count=1 if active_eval else 0,
        failed_count=failed,
        resolved_count=latest.resolved_count if done and latest is not None else 0,
        unresolved_count=latest.unresolved_count if done and latest is not None else 0,
        ignored_patch_mismatch_count=ignored,
        report_paths=tuple(paths),
    )


def build_snapshots(
    run_dir: Path,
    *,
    tasks: list[str] | None = None,
    side_name: str = "official_eval_auto",
    active_generation_tasks: set[str] | None = None,
    active_eval_tasks: set[str] | None = None,
) -> list[TaskSnapshot]:
    predictions = read_jsonl(run_dir / "predictions.jsonl")
    metrics = read_jsonl(run_dir / "metrics.jsonl")
    selected_tasks = tasks or task_ids(predictions, metrics)
    reports = discover_eval_reports(run_dir / side_name)
    active_generation_tasks = active_generation_tasks or set()
    active_eval_tasks = active_eval_tasks or set()
    snapshots: list[TaskSnapshot] = []
    for task_id in selected_tasks:
        pair = latest_paired_rows(predictions, metrics, task_id)
        current_sha = row_patch_sha(pair.prediction)
        active_eval = task_id in active_eval_tasks
        snapshots.append(
            TaskSnapshot(
                task_id=task_id,
                active_generation=task_id in active_generation_tasks,
                active_eval=active_eval,
                prediction=pair.prediction,
                metric=pair.metric,
                metric_pairing=pair.status,
                eval_summary=summarize_eval_reports(
                    reports,
                    task_id=task_id,
                    current_patch_sha=current_sha,
                    active_eval=active_eval,
                ),
            )
        )
    return snapshots


def rows_by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row_task_id(row), []).append(row)
    return grouped
