from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from opencollab.harness.swe_eval_decision import TaskSnapshot, TaskState, decide_task, task_status_row
from opencollab.harness.swe_eval_discovery import (
    EvalReport,
    build_snapshots,
    summarize_eval_reports,
)
from opencollab.harness.swe_eval_records import (
    latest_paired_rows,
    patch_sha,
    row_patch_sha,
)


def _patch(body: str = "+fixed\n") -> str:
    return "diff --git a/pkg/a.py b/pkg/a.py\n@@\n" + body


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_latest_paired_rows_rejects_record_id_patch_sha_mismatch():
    prediction = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "model_patch": _patch("+new\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": patch_sha(_patch("+different\n")),
        "workflow_status": "done",
    }

    pair = latest_paired_rows([prediction], [metric], "task-1")

    assert pair.prediction == prediction
    assert pair.metric is None
    assert pair.status == "record_id_patch_sha_mismatch"


def test_empty_patch_is_terminal_and_not_ready_for_eval():
    prediction = {"instance_id": "task-1", "record_id": "r1", "model_patch": ""}
    snapshot = build_snapshots_from_rows([prediction], [])[0]

    decision = decide_task(snapshot)

    assert decision.state == TaskState.EMPTY_PATCH_INVALID
    assert decision.terminal is True
    assert decision.ready_for_eval is False


def test_matching_done_eval_report_finishes_only_current_patch():
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    reports = [
        EvalReport(
            task_id="task-1",
            patch_sha=patch_sha(_patch("+old\n")),
            status="done",
            resolved_count=1,
            path="old.json",
        ),
        EvalReport(
            task_id="task-1",
            patch_sha=row_patch_sha(prediction),
            status="done",
            unresolved_count=1,
            path="current.json",
        ),
    ]
    snapshot = build_snapshots_from_rows(
        [prediction],
        [metric],
        reports=reports,
    )[0]

    row = task_status_row(snapshot)

    assert row["state"] == "eval_done"
    assert row["eval"]["done_count"] == 1
    assert row["eval"]["ignored_patch_mismatch_count"] == 1
    assert row["eval"]["resolved_count"] == 0
    assert row["eval"]["unresolved_count"] == 1


def test_done_metric_without_matching_eval_report_is_ready_for_eval():
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    reports = [
        EvalReport(
            task_id="task-1",
            patch_sha=patch_sha(_patch("+old\n")),
            status="done",
            resolved_count=1,
            path="old.json",
        )
    ]
    snapshot = build_snapshots_from_rows([prediction], [metric], reports=reports)[0]

    decision = decide_task(snapshot)

    assert decision.state == TaskState.READY_FOR_EVAL
    assert decision.ready_for_eval is True


def test_task_status_row_surfaces_checkpoint_result_from_metric():
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
        "checkpoint_result": {"final": {"status": "written", "loss_bound_seconds": 300}},
    }
    snapshot = build_snapshots_from_rows([prediction], [metric])[0]

    row = task_status_row(snapshot)

    assert row["checkpoint_result"]["final"]["status"] == "written"
    assert row["checkpoint_result"]["final"]["loss_bound_seconds"] == 300


def test_status_script_defaults_to_read_only_summary(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_auto_eval_driver.py"

    result = subprocess.run(
        [sys.executable, str(script), "--run-dir", str(run_dir)],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(result.stdout)
    assert summary["start_eval"] is False
    assert summary["totals"]["ready_for_eval"] == 1
    assert summary["tasks"][0]["state"] == "ready_for_eval"
    assert "actions" not in summary


def test_status_script_dry_run_requires_explicit_start_eval(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_auto_eval_driver.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--run-dir",
            str(run_dir),
            "--start-eval",
            "--dry-run",
            "--eval-command-template",
            "echo {task} {patch_sha}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(result.stdout)
    assert summary["actions"][0]["action"] == "dry_run"
    assert summary["actions"][0]["command"][0] == "echo"


def test_status_script_limits_eval_starts_by_default(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    predictions = []
    metrics = []
    for task in ("task-1", "task-2"):
        prediction = {
            "instance_id": task,
            "record_id": f"{task}-r1",
            "model_patch": _patch("+current\n"),
        }
        predictions.append(prediction)
        metrics.append(
            {
                "instance_id": task,
                "record_id": f"{task}-r1",
                "patch_sha256": row_patch_sha(prediction),
                "workflow_status": "done",
            }
        )
    _write_jsonl(run_dir / "predictions.jsonl", predictions)
    _write_jsonl(run_dir / "metrics.jsonl", metrics)
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_auto_eval_driver.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--run-dir",
            str(run_dir),
            "--start-eval",
            "--dry-run",
            "--eval-command-template",
            "echo {task}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(result.stdout)
    assert summary["totals"]["ready_for_eval"] == 2
    assert len(summary["actions"]) == 1


def test_wave_watchdog_summarizes_runs_config_without_actions(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    runs_config = tmp_path / "runs.json"
    runs_config.write_text(
        json.dumps(
            [
                {
                    "name": "local",
                    "base_run_dir": str(run_dir),
                    "tasks": ["task-1"],
                    "workflow": "single-agent",
                }
            ]
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(runs_config)],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(result.stdout)
    assert summary["schema"] == "opencollab.swe_wave_status.v1"
    assert summary["totals"]["ready_for_eval"] == 1
    assert summary["runs"][0]["tasks"][0]["state"] == "ready_for_eval"


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


def test_build_snapshots_reads_prediction_metric_and_summary_report(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto" / "task-1"
    side_dir.mkdir(parents=True)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    (side_dir / "summary.json").write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "patch_sha256": row_patch_sha(prediction),
                "status": "done",
                "resolved_instances": 1,
            }
        ),
        encoding="utf-8",
    )

    snapshots = build_snapshots(run_dir)

    row = task_status_row(snapshots[0])
    assert row["state"] == "eval_done"
    assert row["eval"]["resolved_count"] == 1
