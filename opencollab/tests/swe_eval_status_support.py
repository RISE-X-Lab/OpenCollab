from __future__ import annotations

import fcntl
import hashlib
import importlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.harness import swe_eval_discovery as discovery_mod
from opencollab.harness import swe_eval_records as records_mod
from opencollab.harness.swe_eval_decision import TaskSnapshot, TaskState, decide_task, task_status_row
from opencollab.harness.swe_eval_discovery import (
    EvalReport,
    build_snapshots,
    discover_eval_reports,
    summarize_eval_reports,
)
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_INELIGIBLE,
    SUBMISSION_INTEGRITY_LEGACY,
    SUBMISSION_INTEGRITY_PROVEN,
    is_completed_prediction,
    latest_paired_rows,
    metric_submission_integrity,
    patch_sha,
    patch_sha_matches,
    row_patch_sha,
)

__all__ = [
    "EvalReport",
    "Path",
    "SUBMISSION_INTEGRITY_INELIGIBLE",
    "SUBMISSION_INTEGRITY_LEGACY",
    "SUBMISSION_INTEGRITY_PROVEN",
    "SimpleNamespace",
    "TaskSnapshot",
    "TaskState",
    "_auto_eval_summary",
    "_patch",
    "_per_instance_run_kwargs",
    "_spawn_normal_exit_with_term_ignoring_descendant",
    "_spawn_term_ignoring_descendant",
    "_strict_modern_prediction",
    "_write_jsonl",
    "_write_ready_eval_pair",
    "build_snapshots",
    "build_snapshots_from_rows",
    "contextmanager",
    "decide_task",
    "discover_eval_reports",
    "discovery_mod",
    "fcntl",
    "hashlib",
    "importlib",
    "io",
    "is_completed_prediction",
    "json",
    "latest_paired_rows",
    "metric_submission_integrity",
    "os",
    "patch_sha",
    "patch_sha_matches",
    "pytest",
    "records_mod",
    "row_patch_sha",
    "signal",
    "stat",
    "subprocess",
    "summarize_eval_reports",
    "sys",
    "task_status_row",
    "threading",
    "time",
]


def _patch(body: str = "+fixed\n") -> str:
    return "diff --git a/pkg/a.py b/pkg/a.py\n@@\n" + body


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_ready_eval_pair(run_dir: Path, task: str = "task-1") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])


def _strict_modern_prediction(*, status="done", returncode=0):
    patch = _patch("+modern\n")
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    metric = {
        "instance_id": "task-1",
        "record_id": "modern-r1",
        "patch_sha256": digest,
        "workflow_status": status,
        "runner_returncode": returncode,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
    }
    return {
        "instance_id": "task-1",
        "record_id": "modern-r1",
        "model_name_or_path": "model",
        "model_patch": patch,
        "patch_sha256": digest,
        "workflow_metric": metric,
    }


def _per_instance_run_kwargs(runner, tmp_path, prediction):
    work_dir = tmp_path / "eval"
    (work_dir / "command_logs").mkdir(parents=True, exist_ok=True)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps([{"instance_id": prediction["instance_id"]}]),
        encoding="utf-8",
    )
    return {
        "iid": prediction["instance_id"],
        "model_name": prediction["model_name_or_path"],
        "identity": runner.prediction_identity(prediction),
        "prediction": prediction,
        "ordinal": 1,
        "total": 1,
        "dataset_path": dataset_path,
        "work_dir": work_dir,
        "run_id": "run",
        "timeout": 10,
        "namespace": "swebench",
        "cache_level": "instance",
        "clean": "False",
        "outer_timeout": 20,
        "env": {},
        "print_lock": threading.Lock(),
    }


def _spawn_term_ignoring_descendant(tmp_path):
    ready = tmp_path / "descendant.ready"
    child_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_code = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
        raise AssertionError("descendant did not become ready")
    return process


def _spawn_normal_exit_with_term_ignoring_descendant(tmp_path):
    ready = tmp_path / "normal-exit-descendant.ready"
    child_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_code = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "[(time.sleep(0.01)) for _ in range(200) if not p.exists()]"
    )
    return subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def build_snapshots_from_rows(
    predictions: list[dict],
    metrics: list[dict],
    *,
    reports: list[EvalReport] | None = None,
):
    run_reports = reports or []
    tasks = {row["instance_id"] for row in [*predictions, *metrics]}
    snapshots = []
    for task_id in sorted(tasks):
        pair = latest_paired_rows(predictions, metrics, task_id)
        active_eval = False
        snapshots.append(
            TaskSnapshot(
                task_id=task_id,
                prediction=pair.prediction,
                metric=pair.metric,
                metric_pairing=pair.status,
                eval_summary=summarize_eval_reports(
                    run_reports,
                    task_id=task_id,
                    current_patch_sha=row_patch_sha(pair.prediction),
                    active_eval=active_eval,
                ),
            )
        )
    return snapshots


def _auto_eval_summary() -> dict:
    return {
        "run_dir": "/tmp/run",
        "side_name": "side",
        "totals": {
            "tasks": 0,
            "ready_for_eval": 0,
            "eval_done": 0,
            "technical_eval_failed": 0,
        },
        "tasks": [],
    }
