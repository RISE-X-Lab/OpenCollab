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


def test_read_jsonl_rejects_oversized_complete_line_without_losing_task(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(records_mod, "MAX_JSONL_LINE_BYTES", 64)
    path = tmp_path / "records.jsonl"
    path.write_bytes(
        b'{"oversized":"'
        + b"x" * 200
        + b'"}\n'
        + b'{"instance_id":"kept"}\n'
    )

    with pytest.raises(records_mod.RecordInputLimitError, match="line exceeds"):
        records_mod.read_jsonl(path)


@pytest.mark.parametrize(
    "bad_line",
    [b'{"broken":}\n', b"\xff\n", b"[]\n"],
)
def test_read_jsonl_rejects_invalid_physical_record(tmp_path, bad_line):
    path = tmp_path / "records.jsonl"
    path.write_bytes(bad_line + b'{"instance_id":"later"}\n')

    with pytest.raises(records_mod.RecordInputFormatError):
        records_mod.read_jsonl(path)


def test_read_jsonl_rejects_rows_over_aggregate_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(records_mod, "MAX_JSONL_RETAINED_ROWS", 2)
    monkeypatch.setattr(records_mod, "MAX_JSONL_RETAINED_BYTES", 1024)
    path = tmp_path / "records.jsonl"
    _write_jsonl(path, [{"n": 1}, {"n": 2}, {"n": 3}])

    with pytest.raises(records_mod.RecordInputLimitError, match="retained row"):
        records_mod.read_jsonl(path)


def test_read_jsonl_rejects_file_over_scan_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(records_mod, "MAX_JSONL_SCAN_BYTES", 32)
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"instance_id":"' + b"x" * 64 + b'"}\n')

    with pytest.raises(records_mod.RecordInputLimitError, match="exceeds 32 bytes"):
        records_mod.read_jsonl(path)


def test_read_jsonl_treats_only_missing_file_as_empty(tmp_path):
    assert records_mod.read_jsonl(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize("mutation", ["shrink", "same_size_rewrite"])
def test_read_jsonl_rejects_file_changed_mid_read(monkeypatch, tmp_path, mutation):
    path = tmp_path / "records.jsonl"
    original = b'{"instance_id":"task"}\n'
    replacement = b'{"instance_id":"evil"}\n'
    assert len(original) == len(replacement)
    path.write_bytes(original)
    original_open = records_mod.open_regular_binary

    class MutatingReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.mutated = False

        def fileno(self):
            return self.wrapped.fileno()

        def readline(self, size=-1):
            value = self.wrapped.readline(size)
            if not self.mutated:
                self.mutated = True
                path.write_bytes(b"" if mutation == "shrink" else replacement)
            return value

    @contextmanager
    def mutating_open(candidate):
        with original_open(candidate) as handle:
            yield MutatingReader(handle)

    monkeypatch.setattr(records_mod, "open_regular_binary", mutating_open)

    with pytest.raises(records_mod.UnsafeRecordInputError, match="changed while reading"):
        records_mod.read_jsonl(path)


def test_read_jsonl_rejects_symlink(tmp_path):
    target = tmp_path / "target.jsonl"
    _write_jsonl(target, [{"instance_id": "must-not-be-read"}])
    link = tmp_path / "records.jsonl"
    link.symlink_to(target)

    with pytest.raises(records_mod.UnsafeRecordInputError):
        records_mod.read_jsonl(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_read_jsonl_rejects_fifo_without_blocking(tmp_path):
    path = tmp_path / "records.jsonl"
    os.mkfifo(path)

    with pytest.raises(records_mod.UnsafeRecordInputError):
        records_mod.read_jsonl(path)


@pytest.mark.skipif(os.name != "posix", reason="character device requires POSIX")
def test_read_jsonl_rejects_character_device():
    with pytest.raises(records_mod.UnsafeRecordInputError):
        records_mod.read_jsonl(Path("/dev/null"))


def test_read_jsonl_rejects_input_over_scan_limit_without_forgetting_old_rows(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(records_mod, "MAX_JSONL_SCAN_BYTES", 80)
    monkeypatch.setattr(records_mod, "MAX_JSONL_LINE_BYTES", 32)
    path = tmp_path / "records.jsonl"
    path.write_bytes(
        b'{"old":"'
        + b"x" * 200
        + b'"}\n'
        + b'{"instance_id":"latest"}\n'
    )

    with pytest.raises(records_mod.RecordInputLimitError, match="exceeds 80 bytes"):
        records_mod.read_jsonl(path)


def test_per_instance_queue_propagates_prediction_scan_limit(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    monkeypatch.setattr(records_mod, "MAX_JSONL_SCAN_BYTES", 64)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps([{"instance_id": "task-1"}]),
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "model_name_or_path": "model",
                "model_patch": _patch("+" + "x" * 100),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputLimitError):
        runner.load_eval_queue(dataset, predictions, "run", tmp_path / "eval")


def test_snapshot_discovery_propagates_prediction_scan_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(records_mod, "MAX_JSONL_SCAN_BYTES", 64)
    (tmp_path / "predictions.jsonl").write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "model_patch": _patch("+" + "x" * 100),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputLimitError):
        build_snapshots(tmp_path)


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


def test_prediction_patch_text_wins_over_stale_explicit_sha():
    current_patch = _patch("+current\n")
    stale_patch = _patch("+stale\n")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": patch_sha(stale_patch),
        "model_patch": current_patch,
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": patch_sha(stale_patch),
        "workflow_status": "done",
    }

    pair = latest_paired_rows([prediction], [metric], "task-1")

    assert row_patch_sha(prediction) == patch_sha(current_patch)
    assert pair.metric is None
    assert pair.status == "record_id_patch_sha_mismatch"


def test_patch_sha_match_rejects_unsafe_short_prefix():
    full = "a" * 64

    assert patch_sha_matches(full, full) is True
    assert patch_sha_matches(full[:12], full) is False
    assert patch_sha_matches(full[:11], full) is False
    assert patch_sha_matches("a", full) is False
    assert patch_sha_matches("g" * 64, "g" * 64) is False


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("done", 0), ("done_with_timeout_patch", 124)],
)
def test_completed_prediction_requires_exact_embedded_identity(status, returncode):
    patch = _patch()
    digest = patch_sha(patch)
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": {
            "instance_id": "task-1",
            "record_id": "attempt-1",
            "patch_sha256": digest,
            "workflow_status": status,
            "runner_returncode": returncode,
        },
    }

    assert is_completed_prediction(row) is True
    assert (
        metric_submission_integrity(row["workflow_metric"])
        == SUBMISSION_INTEGRITY_LEGACY
    )


@pytest.mark.parametrize(
    "field",
    [
        "submission_eligible",
        "execution_quiesced",
        "patch_extraction_succeeded",
        "injected_path_cleanup_proven",
        "harness_artifact_exclusion_proven",
        "checkpoint_restore_integrity_proven",
        "task_stage_integrity_proven",
    ],
)
def test_completed_prediction_rejects_explicit_false_integrity_field(field):
    patch = _patch()
    digest = patch_sha(patch)
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
        field: False,
    }
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": metric,
    }

    assert metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE
    assert is_completed_prediction(row) is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda metric: metric.update(test_patch_isolation_failed=True),
        lambda metric: metric.update(submission_eligible=0),
        lambda metric: metric.update(worktree_integrity_proven=False),
        lambda metric: metric.update(patch_produced=False),
        lambda metric: metric.update(
            checkpoint_result={"worktree_integrity_proven": False}
        ),
        lambda metric: metric.update(
            checkpoint_result={
                "restore": {
                    "status": "failed",
                    "submission_eligible": False,
                    "worktree_integrity_proven": False,
                },
                "final": {"submission_eligible": True},
            }
        ),
        lambda metric: metric.update(
            checkpoint_result={
                "restore": {
                    "status": "restored",
                    "submission_eligible": False,
                    "worktree_integrity_proven": True,
                }
            }
        ),
    ],
)
def test_completed_prediction_rejects_other_explicit_integrity_failures(mutation):
    patch = _patch()
    digest = patch_sha(patch)
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    mutation(metric)
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": metric,
    }

    assert metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE
    assert is_completed_prediction(row) is False


def test_completed_prediction_accepts_fully_proven_integrity_fields():
    patch = _patch()
    digest = patch_sha(patch)
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
    }
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": metric,
    }

    assert metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_PROVEN
    assert is_completed_prediction(row) is True


@pytest.mark.parametrize(
    "checkpoint_result",
    [
        {"final": {"status": "failed", "submission_eligible": False}},
        {
            "restore": {
                "status": "skipped_not_submission_eligible",
                "submission_eligible": False,
                "worktree_integrity_proven": True,
            }
        },
    ],
)
def test_completed_prediction_rejects_partial_modern_checkpoint_fields(
    checkpoint_result,
):
    patch = _patch()
    digest = patch_sha(patch)
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
        "checkpoint_result": checkpoint_result,
    }
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": metric,
    }

    assert metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE
    assert is_completed_prediction(row) is False


@pytest.mark.parametrize(
    "partial_fields",
    [
        {"submission_eligible": True},
        {
            "submission_eligible": True,
            "execution_quiesced": True,
            "patch_extraction_succeeded": True,
            "injected_path_cleanup_proven": True,
            "harness_artifact_exclusion_proven": True,
            "checkpoint_restore_integrity_proven": True,
            # task_stage_integrity_proven is missing.
            "test_patch_isolation_failed": False,
        },
        {"submission_eligble": True},
    ],
)
def test_completed_prediction_rejects_partial_or_misspelled_modern_proof(
    partial_fields,
):
    patch = _patch()
    digest = patch_sha(patch)
    metric = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
        **partial_fields,
    }
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": metric,
    }

    assert metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE
    assert is_completed_prediction(row) is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(model_patch=""),
        lambda row: row.update(record_id="attempt-2"),
        lambda row: row.update(patch_sha256="a" * 64),
        lambda row: row["workflow_metric"].update(instance_id="task-2"),
        lambda row: row["workflow_metric"].update(record_id="attempt-2"),
        lambda row: row["workflow_metric"].update(patch_sha256="b" * 64),
        lambda row: row["workflow_metric"].update(workflow_status="error"),
        lambda row: row["workflow_metric"].update(runner_returncode=1),
        lambda row: row["workflow_metric"].update(runner_returncode=True),
    ],
)
def test_completed_prediction_rejects_incomplete_or_mismatched_rows(mutation):
    patch = _patch()
    digest = patch_sha(patch)
    row = {
        "instance_id": "task-1",
        "record_id": "attempt-1",
        "patch_sha256": digest,
        "model_patch": patch,
        "workflow_metric": {
            "instance_id": "task-1",
            "record_id": "attempt-1",
            "patch_sha256": digest,
            "workflow_status": "done",
            "runner_returncode": 0,
        },
    }

    mutation(row)

    assert is_completed_prediction(row) is False


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
    row = task_status_row(snapshot)

    assert decision.state == TaskState.READY_FOR_EVAL
    assert decision.ready_for_eval is True
    assert "legacy eligibility compatibility" in decision.reason
    assert row["submission_integrity"] == SUBMISSION_INTEGRITY_LEGACY


def test_explicitly_ineligible_metric_cannot_be_ready_or_eval_done():
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
        "runner_returncode": 0,
        "submission_eligible": False,
    }
    reports = [
        EvalReport(
            task_id="task-1",
            patch_sha=row_patch_sha(prediction),
            status="done",
            resolved_count=1,
            path="invalid-eval.json",
        )
    ]
    snapshot = build_snapshots_from_rows([prediction], [metric], reports=reports)[0]

    decision = decide_task(snapshot)
    row = task_status_row(snapshot)

    assert decision.state == TaskState.WORKFLOW_FAILED
    assert decision.ready_for_eval is False
    assert decision.terminal is True
    assert row["submission_integrity"] == SUBMISSION_INTEGRITY_INELIGIBLE


def test_matching_done_eval_report_supersedes_earlier_infra_failure():
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
            patch_sha=row_patch_sha(prediction),
            status="technical_eval_failed",
            path="first-docker-refused.json",
        ),
        EvalReport(
            task_id="task-1",
            patch_sha=row_patch_sha(prediction),
            status="done",
            resolved_count=1,
            path="rerun-resolved.json",
        ),
    ]
    snapshot = build_snapshots_from_rows([prediction], [metric], reports=reports)[0]

    row = task_status_row(snapshot)

    assert row["state"] == "eval_done"
    assert row["eval"]["done_count"] == 1
    assert row["eval"]["failed_count"] == 0
    assert row["eval"]["resolved_count"] == 1
    assert row["eval"]["report_paths"] == ["rerun-resolved.json"]


def test_later_infra_failure_supersedes_earlier_done_report():
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
            patch_sha=row_patch_sha(prediction),
            status="done",
            unresolved_count=1,
            path="old-unclassified.json",
        ),
        EvalReport(
            task_id="task-1",
            patch_sha=row_patch_sha(prediction),
            status="technical_eval_failed",
            path="classified-redis.json",
        ),
    ]
    snapshot = build_snapshots_from_rows([prediction], [metric], reports=reports)[0]

    row = task_status_row(snapshot)

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["done_count"] == 0
    assert row["eval"]["failed_count"] == 1
    assert row["eval"]["unresolved_count"] == 0
    assert row["eval"]["report_paths"] == ["classified-redis.json"]


def test_eval_report_without_patch_sha_does_not_finish_current_patch():
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
            patch_sha="",
            status="done",
            resolved_count=1,
            path="old-without-sha.json",
        )
    ]
    snapshot = build_snapshots_from_rows([prediction], [metric], reports=reports)[0]

    row = task_status_row(snapshot)

    assert row["state"] == "ready_for_eval"
    assert row["eval"]["done_count"] == 0
    assert row["eval"]["ignored_patch_mismatch_count"] == 1


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


def test_auto_eval_claim_allows_only_one_concurrent_start(monkeypatch, tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    entered = threading.Event()
    release = threading.Event()

    class FakeProcess:
        pid = os.getpid()

    def fake_popen(command, cwd, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return FakeProcess()

    monkeypatch.setattr(driver, "_process_start_identity", lambda pid: "proc:test")
    monkeypatch.setattr(driver.subprocess, "Popen", fake_popen)
    args = SimpleNamespace(
        start_eval=True,
        eval_command_template="echo {task}",
        max_eval_starts=1,
        dry_run=False,
        run_dir=tmp_path,
        side_name="official_eval_auto",
    )
    summary = {
        "tasks": [
            {
                "task": "task-1",
                "record_id": "record-1",
                "patch_sha256": "a" * 64,
                "ready_for_eval": True,
            }
        ]
    }
    results = []

    first = threading.Thread(target=lambda: results.append(driver.maybe_start_eval(args, summary)))
    first.start()
    assert entered.wait(timeout=2)
    second = threading.Thread(target=lambda: results.append(driver.maybe_start_eval(args, summary)))
    second.start()
    second.join(timeout=2)
    release.set()
    first.join(timeout=2)

    actions = [item[0]["action"] for item in results]
    assert sorted(actions) == ["already_claimed", "started"]


def test_auto_eval_does_not_reclaim_fresh_partial_claim(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim_path = tmp_path / "claim.json"
    claim_path.write_text("", encoding="utf-8")

    acquired, existing = driver._acquire_claim(
        claim_path,
        {"schema": "opencollab.swe_eval_claim.v1", "pid": os.getpid()},
    )

    assert acquired is False
    assert existing == {"status": "claim_in_progress"}


def test_auto_eval_claim_publish_is_serialized(monkeypatch, tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    entered = threading.Event()
    release = threading.Event()
    original_write = driver._write_bytes_atomic_at
    write_count = 0
    count_lock = threading.Lock()

    def blocking_write(parent_fd, name, payload, *, label):
        nonlocal write_count
        with count_lock:
            write_count += 1
            first = write_count == 1
        if first:
            entered.set()
            release.wait(timeout=2)
        return original_write(parent_fd, name, payload, label=label)

    monkeypatch.setattr(driver, "_write_bytes_atomic_at", blocking_write)
    claim_path = tmp_path / "claim.json"
    results = []

    first = threading.Thread(
        target=lambda: results.append(
            driver._acquire_claim(claim_path, {"pid": os.getpid(), "owner": "first"})
        )
    )
    second = threading.Thread(
        target=lambda: results.append(
            driver._acquire_claim(claim_path, {"pid": os.getpid(), "owner": "second"})
        )
    )
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(results) == 2
    assert sum(1 for acquired, _ in results if acquired) == 1
    assert json.loads(claim_path.read_text(encoding="utf-8"))["owner"] == "first"


def test_auto_eval_start_failure_returns_nonzero(tmp_path):
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
            "--eval-command-template",
            "/definitely/missing/eval-command {task}",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["actions"][0]["action"] == "failed_to_start"


def test_auto_eval_detaches_child_output_and_returns_before_child(tmp_path):
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
    command = (
        f"{sys.executable} -c "
        "'import time; print(\"CHILD_OUTPUT\"); time.sleep(5)'"
    )

    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--run-dir",
            str(run_dir),
            "--start-eval",
            "--eval-command-template",
            command,
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=3,
    )
    elapsed = time.monotonic() - started
    summary = json.loads(result.stdout)
    child_pid = summary["actions"][0]["pid"]
    try:
        os.killpg(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    assert elapsed < 2
    assert summary["actions"][0]["action"] == "started"
    assert Path(summary["actions"][0]["log"]).is_file()


def test_auto_eval_wrapper_kills_background_group_after_leader_exit(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim_path = tmp_path / "claim.json"
    attempt_path = tmp_path / "attempt.json"
    sentinel = tmp_path / "late-write"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(1.5); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    leader_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(0.05)"
    )
    identity = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
        "started_at_ns": time.time_ns(),
        "status": "launching",
        "pid": 0,
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            driver._EVAL_WRAPPER,
            str(claim_path),
            str(attempt_path),
            json.dumps({**identity, "schema": "opencollab.swe_eval_claim.v1"}),
            json.dumps(identity),
            json.dumps([sys.executable, "-c", leader_code]),
        ],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 197
    assert not sentinel.exists()
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert claim["status"] == "technical_eval_failed"
    assert attempt["status"] == "technical_eval_failed"
    assert attempt["evaluator_returncode"] == 0


def test_auto_eval_wrapper_signal_terminates_owned_evaluator_group(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim_path = tmp_path / "claim.json"
    attempt_path = tmp_path / "attempt.json"
    sentinel = tmp_path / "signal-leak"
    command_code = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    identity = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
        "started_at_ns": time.time_ns(),
        "status": "launching",
        "pid": 0,
    }
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            driver._EVAL_WRAPPER,
            str(claim_path),
            str(attempt_path),
            json.dumps({**identity, "schema": "opencollab.swe_eval_claim.v1"}),
            json.dumps(identity),
            json.dumps([sys.executable, "-c", command_code]),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if claim_path.exists():
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
                if claim.get("status") == "started":
                    break
            time.sleep(0.01)
        else:
            pytest.fail("wrapper did not publish its child identity")

        os.kill(wrapper.pid, signal.SIGTERM)
        time.sleep(0.01)
        try:
            os.kill(wrapper.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        assert wrapper.wait(timeout=4) == 128 + signal.SIGTERM
        time.sleep(1.6)
        assert not sentinel.exists()
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        assert attempt["status"] == "technical_eval_failed"
        assert attempt["cleanup_quiesced"] is True
    finally:
        if wrapper.poll() is None:
            try:
                os.killpg(wrapper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            wrapper.wait(timeout=2)


def test_auto_eval_claim_retains_live_residual_evaluator_group(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        claim_path = tmp_path / "claim.json"
        claim_path.write_text(
            json.dumps(
                {
                    "schema": "opencollab.swe_eval_claim.v1",
                    "pid": 0,
                    "status": "residual_process_group",
                    "evaluator_pgid": process.pid,
                    "evaluator_start_identity": driver._process_start_identity(
                        process.pid
                    ),
                }
            ),
            encoding="utf-8",
        )

        acquired, existing = driver._acquire_claim(
            claim_path,
            {"schema": "opencollab.swe_eval_claim.v1", "pid": os.getpid()},
        )

        assert acquired is False
        assert existing["evaluator_pgid"] == process.pid
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def test_technical_attempt_blocks_immediate_auto_eval_retry(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    run_dir = tmp_path / "run"
    side_name = "official_eval_auto"
    attempt_dir = run_dir / side_name / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    patch_digest = row_patch_sha(prediction)
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": patch_digest,
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": patch_digest,
                "started_at_ns": time.time_ns(),
                "status": "technical_eval_failed",
                "pid": 0,
            }
        ),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir, side_name=side_name)[0])
    assert row["state"] == "technical_eval_failed"
    assert row["ready_for_eval"] is False

    args = SimpleNamespace(
        start_eval=True,
        eval_command_template="echo {task}",
        max_eval_starts=1,
        dry_run=False,
        run_dir=run_dir,
        side_name=side_name,
    )
    assert driver.maybe_start_eval(args, {"tasks": [row]}) == []


def test_completed_attempt_without_report_is_technical_failure(tmp_path):
    run_dir = tmp_path / "run"
    side_name = "official_eval_auto"
    attempt_dir = run_dir / side_name / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_patch": _patch("+current\n"),
    }
    patch_digest = row_patch_sha(prediction)
    metric = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": patch_digest,
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": patch_digest,
                "started_at_ns": time.time_ns(),
                "status": "completed",
                "pid": 0,
            }
        ),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir, side_name=side_name)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["ready_for_eval"] is False


def test_launching_attempt_binds_report_written_after_launch(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    report_dir = side_dir / "reports" / "task-1"
    attempt_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
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
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": 1,
                "status": "launching",
                "pid": 0,
                "prior_reports": {},
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "report.json").write_text(
        json.dumps({"task-1": {"resolved": True}}),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "eval_done"
    assert row["eval"]["resolved_count"] == 1


def test_started_attempt_with_live_pid_is_eval_active(tmp_path):
    run_dir = tmp_path / "run"
    attempt_dir = run_dir / "official_eval_auto" / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
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
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": time.time_ns(),
                "status": "started",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "eval_active"
    assert row["eval"]["active_count"] == 1


def test_started_attempt_with_reused_pid_identity_is_technical(
    monkeypatch,
    tmp_path,
):
    run_dir = tmp_path / "run"
    _write_ready_eval_pair(run_dir)
    attempt_dir = run_dir / "official_eval_auto" / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(
                    records_mod.read_jsonl(run_dir / "predictions.jsonl")[0]
                ),
                "started_at_ns": time.time_ns(),
                "status": "started",
                "pid": os.getpid(),
                "owner_start_identity": "old-process-start",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        discovery_mod,
        "_process_start_identity",
        lambda pid: "reused-process-start",
    )

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["active_count"] == 0
    assert row["eval"]["failed_count"] == 1


def test_stale_legacy_attempt_without_start_identity_is_technical(tmp_path):
    run_dir = tmp_path / "run"
    _write_ready_eval_pair(run_dir)
    prediction = records_mod.read_jsonl(run_dir / "predictions.jsonl")[0]
    attempt_dir = run_dir / "official_eval_auto" / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "r1.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": 1,
                "status": "started",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["active_count"] == 0
    assert row["eval"]["failed_count"] == 1


def test_per_instance_queue_rejects_stale_standard_report(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.jsonl"
    work_dir = tmp_path / "eval"
    prediction = {
        "instance_id": "task-1",
        "record_id": "new-record",
        "model_name_or_path": "model",
        "model_patch": _patch("+new\n"),
    }
    dataset_path.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    _write_jsonl(predictions_path, [prediction])
    report = runner.report_path(work_dir, "run", "model", "task-1")
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"task-1": {"resolved": True}}), encoding="utf-8")

    queue = runner.load_eval_queue(dataset_path, predictions_path, "run", work_dir)

    assert [item[0] for item in queue] == ["task-1"]


def test_per_instance_queue_accepts_sidecar_for_exact_candidate(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.jsonl"
    work_dir = tmp_path / "eval"
    prediction = {
        "instance_id": "task-1",
        "record_id": "current-record",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    dataset_path.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    _write_jsonl(predictions_path, [prediction])
    report = runner.report_path(work_dir, "run", "model", "task-1")
    identity = runner.prediction_identity(prediction)
    attempt = runner.write_identity(runner.identity_path(report), identity)
    report.write_text(json.dumps({"task-1": {"resolved": False}}), encoding="utf-8")
    os.utime(
        report,
        ns=(attempt["started_at_ns"] + 1, attempt["started_at_ns"] + 1),
    )

    queue = runner.load_eval_queue(dataset_path, predictions_path, "run", work_dir)

    assert queue == []


def test_per_instance_queue_retries_exact_candidate_after_technical_report(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.jsonl"
    work_dir = tmp_path / "eval"
    prediction = {
        "instance_id": "task-1",
        "record_id": "current-record",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    dataset_path.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    _write_jsonl(predictions_path, [prediction])
    report = runner.report_path(work_dir, "run", "model", "task-1")
    identity = runner.prediction_identity(prediction)
    attempt = runner.write_identity(runner.identity_path(report), identity)
    report.write_text(
        json.dumps(
            {
                "task-1": {
                    "status": "technical_eval_failed",
                    "resolved": False,
                    "error": "docker daemon unavailable",
                    "patch_sha256": identity["patch_sha256"],
                }
            }
        ),
        encoding="utf-8",
    )
    os.utime(
        report,
        ns=(attempt["started_at_ns"] + 1, attempt["started_at_ns"] + 1),
    )

    queue = runner.load_eval_queue(dataset_path, predictions_path, "run", work_dir)

    assert [item[0] for item in queue] == ["task-1"]
    assert runner.report_is_done(report, "task-1", identity) is False


def test_discovery_honors_per_instance_prior_report_fingerprint(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"task-1": {"resolved": True}}),
        encoding="utf-8",
    )
    future_ns = time.time_ns() + 5_000_000_000
    os.utime(report, ns=(future_ns, future_ns))
    identity = {
        "instance_id": "task-1",
        "record_id": "new-record",
        "patch_sha256": "b" * 64,
    }
    runner.write_identity(
        runner.identity_path(report),
        identity,
        status="started",
        pid=0,
        started_at_ns=time.time_ns(),
        prior_report_fingerprint=runner.file_fingerprint(report),
    )

    reports = discover_eval_reports(tmp_path)

    assert reports[0].patch_sha == ""
    assert reports[0].record_id == ""


def test_completed_attempt_with_reused_live_pid_is_not_active_and_binds_report(tmp_path):
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
    _write_jsonl(tmp_path / "predictions.jsonl", [prediction])
    _write_jsonl(tmp_path / "metrics.jsonl", [metric])
    report = tmp_path / "eval" / "task-1" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps({"task-1": {"resolved": True}}),
        encoding="utf-8",
    )
    (report.parent / "opencollab-attempt.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": time.time_ns(),
                "status": "completed",
                "pid": os.getpid(),
                "prior_report_fingerprint": "",
            }
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshots(tmp_path, side_name="eval")[0]
    decision = decide_task(snapshot)

    assert snapshot.active_eval is False
    assert decision.state == TaskState.EVAL_DONE


def test_per_instance_reader_rejects_crash_truncated_jsonl_tail(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    path = tmp_path / "predictions.jsonl"
    path.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+x"})
        + "\n"
        + '{"instance_id":',
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="invalid JSONL"):
        runner.read_jsonl(path)


def test_per_instance_queue_accepts_jsonl_dataset(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "instances.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    dataset_path.write_text(
        json.dumps({"instance_id": "task-1"})
        + "\n"
        + json.dumps({"instance_id": "task-2"})
        + "\n",
        encoding="utf-8",
    )
    _write_jsonl(
        predictions_path,
        [
            {
                "instance_id": task,
                "record_id": f"{task}-r1",
                "model_name_or_path": "model",
                "model_patch": _patch(f"+{task}\n"),
            }
            for task in ("task-1", "task-2")
        ],
    )

    queue = runner.load_eval_queue(dataset_path, predictions_path, "run", tmp_path / "eval")

    assert [item[0] for item in queue] == ["task-1", "task-2"]


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


def test_per_instance_queue_accepts_strict_modern_prediction(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    _write_jsonl(predictions, [_strict_modern_prediction()])

    queue = runner.load_eval_queue(dataset, predictions, "run", tmp_path / "eval")

    assert [item[0] for item in queue] == ["task-1"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row["workflow_metric"].pop("execution_quiesced"),
        lambda row: row["workflow_metric"].update(workflow_status="error"),
        lambda row: row["workflow_metric"].update(runner_returncode=1),
        lambda row: row["workflow_metric"].update(patch_sha256="0" * 64),
        lambda row: row.update(patch_sha256="f" * 64),
    ],
)
def test_per_instance_queue_rejects_invalid_modern_prediction(
    tmp_path,
    mutate,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    row = _strict_modern_prediction()
    mutate(row)
    _write_jsonl(predictions, [row])

    queue = runner.load_eval_queue(dataset, predictions, "run", tmp_path / "eval")

    assert queue == []


def test_per_instance_queue_keeps_plain_legacy_prediction_compatible(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text(json.dumps([{"instance_id": "task-1"}]), encoding="utf-8")
    _write_jsonl(
        predictions,
        [
            {
                "instance_id": "task-1",
                "record_id": "legacy-r1",
                "model_name_or_path": "model",
                "model_patch": _patch("+legacy\n"),
            }
        ],
    )

    queue = runner.load_eval_queue(dataset, predictions, "run", tmp_path / "eval")

    assert [item[0] for item in queue] == ["task-1"]


def test_per_instance_dataset_rejects_truncated_jsonl_tail(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    path = tmp_path / "instances.jsonl"
    path.write_text(
        json.dumps({"instance_id": "task-1"}) + "\n" + '{"instance_id":',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid JSONL"):
        runner.read_dataset(path)


@pytest.mark.parametrize(
    "unsafe_identity",
    [
        "/tmp/escape",
        "C:escape",
        "..",
        "a\\b",
        "bad\nname",
        "bad\u200dname",
        "\ud800",
        "x" * 241,
    ],
)
@pytest.mark.parametrize(
    "field",
    ["run_id", "model_name_or_path", "instance_id"],
)
def test_per_instance_report_path_rejects_unsafe_identity(
    tmp_path,
    field,
    unsafe_identity,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    values = {
        "run_id": "run",
        "model_name_or_path": "model",
        "instance_id": "task-1",
    }
    values[field] = unsafe_identity

    with pytest.raises(ValueError):
        runner.report_path(
            tmp_path,
            values["run_id"],
            values["model_name_or_path"],
            values["instance_id"],
        )

    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize("field", ["run_id", "instance_id"])
def test_per_instance_report_path_rejects_unencoded_separator(tmp_path, field):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    values = {
        "run_id": "run",
        "model_name_or_path": "model",
        "instance_id": "task-1",
    }
    values[field] = "a/b"

    with pytest.raises(ValueError, match="path separators"):
        runner.report_path(
            tmp_path,
            values["run_id"],
            values["model_name_or_path"],
            values["instance_id"],
        )


def test_per_instance_report_path_preserves_official_model_encoding(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    path = runner.report_path(tmp_path, "run", "org/model", "task-1")

    assert path == (
        tmp_path
        / "logs"
        / "run_evaluation"
        / "run"
        / "org__model"
        / "task-1"
        / "report.json"
    )


@pytest.mark.parametrize(
    "model_name",
    ["org//model", "org/./model", "org/../model", "org/model/"],
)
def test_per_instance_report_path_rejects_unsafe_model_segments(
    tmp_path,
    model_name,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    with pytest.raises(ValueError, match="empty or dot path segments"):
        runner.report_path(tmp_path, "run", model_name, "task-1")


@pytest.mark.parametrize(
    "field",
    ["run_id", "model_name_or_path", "instance_id"],
)
def test_per_instance_queue_rejects_path_traversal_before_artifact_write(
    tmp_path,
    field,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.jsonl"
    instance_id = "../escape" if field == "instance_id" else "task-1"
    model_name = "../escape" if field == "model_name_or_path" else "model"
    run_id = "../escape" if field == "run_id" else "run"
    dataset_path.write_text(
        json.dumps([{"instance_id": instance_id}]),
        encoding="utf-8",
    )
    _write_jsonl(
        predictions_path,
        [
            {
                "instance_id": instance_id,
                "record_id": "r1",
                "model_name_or_path": model_name,
                "model_patch": _patch("+current\n"),
            }
        ],
    )
    work_dir = tmp_path / "eval"

    with pytest.raises(ValueError):
        runner.load_eval_queue(
            dataset_path,
            predictions_path,
            run_id,
            work_dir,
        )

    assert not work_dir.exists()
    assert not (tmp_path / "escape").exists()


def test_per_instance_main_rejects_unsafe_run_id_before_workdir_write(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(tmp_path / "missing-dataset.json"),
            "--predictions",
            str(tmp_path / "missing-predictions.jsonl"),
            "--work-dir",
            str(work_dir),
            "--run-id",
            "../escape",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "1.5"),
        ("--workers", "0"),
        ("--workers", "nan"),
        ("--limit", "-1"),
        ("--outer-timeout", "-1"),
        ("--outer-timeout", "1.5"),
    ],
)
def test_per_instance_main_strictly_rejects_invalid_numeric_arguments(
    monkeypatch,
    tmp_path,
    flag,
    value,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(tmp_path / "missing-dataset.json"),
            "--predictions",
            str(tmp_path / "missing-predictions.jsonl"),
            "--work-dir",
            str(work_dir),
            "--run-id",
            "run",
            flag,
            value,
        ],
    )

    with pytest.raises(SystemExit) as captured:
        runner.main()

    assert captured.value.code == 2
    assert not work_dir.exists()


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


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX fork")
def test_per_instance_permanently_blocked_popen_helper_is_reaped(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    helper_pid = tmp_path / "helper.pid"

    def block_forever(*args, **kwargs):
        del args, kwargs
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        helper_pid.write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(1)

    monkeypatch.setattr(runner, "_EVALUATOR_POPEN", block_forever)
    monkeypatch.setattr(runner, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.2)
    log = tmp_path / "eval.log"
    started = time.monotonic()
    with log.open("a", encoding="utf-8") as handle:
        with pytest.raises(runner.EvaluatorSpawnTimeout):
            runner._spawn_owned_evaluator(
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                env=os.environ.copy(),
                log_fd=handle.fileno(),
                wall_timeout=0.2,
                spawn_timeout=0.05,
            )

    assert time.monotonic() - started < 1.0
    pid = int(helper_pid.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(
    os.name != "posix" or Path("/proc").is_dir(),
    reason="non-Linux identity helper path",
)
def test_per_instance_identity_popen_block_is_bounded_and_reaped(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    helper_pid = tmp_path / "identity-helper.pid"

    def block_forever(*args, **kwargs):
        del args, kwargs
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        helper_pid.write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(1)

    monkeypatch.setattr(runner, "_PROCESS_IDENTITY_POPEN", block_forever)
    monkeypatch.setattr(runner, "PROCESS_IDENTITY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runner, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()

    assert runner.process_start_identity(os.getpid()) == ""

    assert time.monotonic() - started < 1.0
    pid = int(helper_pid.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX sessions")
def test_per_instance_helper_normal_exit_cleans_lingering_descendant(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    started = tmp_path / "owned.started"
    finished = tmp_path / "owned.finished"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(started)!r}).touch();"
        "time.sleep(0.8);"
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "time.sleep(0.1)"
    )
    log = tmp_path / "eval.log"
    with log.open("a", encoding="utf-8") as handle:
        process = runner._spawn_owned_evaluator(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env=os.environ.copy(),
            log_fd=handle.fileno(),
            wall_timeout=1.0,
            spawn_timeout=0.2,
        )
        assert process.wait(timeout=1.0) == 0
        assert runner.ensure_process_group_quiesced_after_wait(process, handle) is True

    assert started.exists()
    time.sleep(0.9)
    assert not finished.exists()


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX wall bound")
def test_per_instance_helper_wait_uses_total_wall_deadline(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    log = tmp_path / "eval.log"
    started = time.monotonic()
    with log.open("a", encoding="utf-8") as handle:
        process = runner._spawn_owned_evaluator(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            log_fd=handle.fileno(),
            wall_timeout=0.15,
            spawn_timeout=0.1,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        assert runner.terminate_process_group(
            process,
            handle,
            term_timeout=0.05,
            kill_timeout=0.2,
        ) is True

    assert time.monotonic() - started < 1.0


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


def test_per_instance_uses_immutable_candidate_and_requires_report(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    commands = []

    class FakeProcess:
        pid = 999_999_902

        def wait(self, timeout=None):
            report = runner.report_path(
                kwargs["work_dir"], "run", "model", "task-1"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"task-1": {"resolved": True}}),
                encoding="utf-8",
            )
            return 0

    def fake_popen(command, **popen_kwargs):
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    result = runner.run_one(**kwargs)
    candidate_path = Path(commands[0][commands[0].index("-p") + 1])

    assert result == ("task-1", 0)
    assert candidate_path != tmp_path / "predictions.jsonl"
    assert runner.read_jsonl(candidate_path) == [prediction]
    attempt = json.loads(
        runner.identity_path(
            runner.report_path(kwargs["work_dir"], "run", "model", "task-1")
        ).read_text(encoding="utf-8")
    )
    assert attempt["status"] == "completed"
    assert attempt["pid"] == 0


def test_per_instance_claim_blocks_concurrent_duplicate(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    entered = threading.Event()
    release = threading.Event()
    popen_count = 0

    class FakeProcess:
        pid = 999_999_903

        def wait(self, timeout=None):
            entered.set()
            release.wait(timeout=2)
            report = runner.report_path(
                kwargs["work_dir"], "run", "model", "task-1"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"task-1": {"resolved": False}}),
                encoding="utf-8",
            )
            return 0

    def fake_popen(command, **popen_kwargs):
        nonlocal popen_count
        popen_count += 1
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    results = []
    first = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    second = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    release.set()
    first.join(timeout=2)

    assert popen_count == 1
    assert sorted(results) == [("task-1", 0), ("task-1", 0)]


def test_per_instance_zero_exit_without_report_is_failure(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)

    class FakeProcess:
        pid = 999_999_904

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    assert runner.run_one(**kwargs) == ("task-1", 3)


def test_per_instance_started_identity_failure_terminates_child(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    original_write_identity = runner.write_identity
    identity_writes = 0
    signals = []

    def flaky_write_identity(*args, **write_kwargs):
        nonlocal identity_writes
        identity_writes += 1
        if identity_writes >= 2:
            raise OSError("disk unavailable")
        return original_write_identity(*args, **write_kwargs)

    class FakeProcess:
        pid = 424242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner, "write_identity", flaky_write_identity)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = runner.run_one(**kwargs)

    assert result == ("task-1", 4)
    assert signals == [(424242, signal.SIGTERM)]
    assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()


def test_per_instance_kill_reap_timeout_is_bounded_and_consumed(monkeypatch):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    release = threading.Event()
    consumer_started = threading.Event()
    consumer_finished = threading.Event()
    waits = []
    signals = []

    class StubbornProcess:
        pid = 424243

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            consumer_finished.set()
            return 0

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    log = io.StringIO()

    reaped = runner.terminate_process_group(
        StubbornProcess(),
        log,
        term_timeout=0.001,
        kill_timeout=0.001,
    )

    assert reaped is False
    assert signals == [
        (424243, signal.SIGTERM),
        (424243, signal.SIGKILL),
    ]
    assert len(waits) >= 2
    assert all(0 <= timeout <= 0.001 for timeout in waits[:2])
    assert consumer_started.wait(timeout=0.2)
    assert "technical cleanup failure" in log.getvalue()
    release.set()
    assert consumer_finished.wait(timeout=0.2)


def test_per_instance_cleanup_kills_descendant_after_leader_exits(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = runner.terminate_process_group(
            process,
            io.StringIO(),
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert runner._process_group_exists(process.pid) is False
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_per_instance_normal_exit_cleans_residual_descendants(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    real_terminate = runner.terminate_process_group

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        runner,
        "terminate_process_group",
        lambda child, log: real_terminate(
            child,
            log,
            term_timeout=0.05,
            kill_timeout=1.0,
        ),
    )
    try:
        result = runner.run_one(**kwargs)

        assert result == ("task-1", 3)
        assert runner._process_group_exists(process.pid) is False
        assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()
        log = kwargs["work_dir"] / "command_logs" / "task-1.log"
        assert "residual process group" in log.read_text(encoding="utf-8")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_per_instance_normal_exit_cleanup_failure_retains_claim(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)

    class ReapedLeader:
        pid = 424261

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    monkeypatch.setattr(runner, "_process_group_exists", lambda pgid: True)
    monkeypatch.setattr(runner, "terminate_process_group", lambda *args: False)
    monkeypatch.setattr(runner, "process_start_identity", lambda pid: f"start-{pid}")

    result = runner.run_one(**kwargs)
    claim = json.loads(
        runner._claim_path(kwargs["work_dir"], "task-1").read_text(
            encoding="utf-8"
        )
    )

    assert result == ("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)
    assert claim["status"] == "cleanup_failed"
    assert claim["evaluator_pgid"] == 424261


def test_per_instance_owned_cleanup_defers_repeated_interrupts():
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    class DoubleInterruptDone:
        calls = 0

        def is_set(self):
            return self.calls >= 3

        def wait(self, timeout):
            self.calls += 1
            if self.calls <= 2:
                raise KeyboardInterrupt(f"cancel-{self.calls}")
            return True

    completed, interruption = runner._wait_for_owned_cleanup(
        DoubleInterruptDone(),
        timeout=0.2,
    )

    assert completed is True
    assert isinstance(interruption, KeyboardInterrupt)
    assert interruption.args == ("cancel-1",)


def test_per_instance_timeout_cleanup_re_raises_caller_interrupt_after_reap(
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    waits = []
    signals = []

    class CooperativeProcess:
        pid = 424245

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

    real_wait = runner._wait_for_owned_cleanup

    def interrupted_wait(done, *, timeout):
        completed, _interruption = real_wait(done, timeout=timeout)
        return completed, KeyboardInterrupt("caller cancelled during timeout cleanup")

    monkeypatch.setattr(runner, "_wait_for_owned_cleanup", interrupted_wait)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="caller cancelled"):
        runner.terminate_process_group(
            CooperativeProcess(),
            io.StringIO(),
            term_timeout=0.01,
            kill_timeout=0.01,
        )

    assert signals == [(424245, signal.SIGTERM)]
    assert len(waits) == 1
    assert 0 <= waits[0] <= 0.01


def test_per_instance_outer_timeout_reports_unreaped_cleanup(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    release = threading.Event()

    class StubbornProcess:
        pid = 424244

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *args, **kwargs: None)
    try:
        result = runner.run_one(**kwargs)
    finally:
        release.set()

    assert result == ("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)
    log = kwargs["work_dir"] / "command_logs" / "task-1.log"
    assert "technical cleanup failure" in log.read_text(encoding="utf-8")
    claim_path = runner._claim_path(kwargs["work_dir"], "task-1")
    assert claim_path.exists()
    assert json.loads(claim_path.read_text(encoding="utf-8"))["owner_token"]


def test_per_instance_wait_interrupt_terminates_child_and_re_raises(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    signals = []

    class InterruptedProcess:
        pid = 424247

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt("interrupt during outer wait")
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="outer wait"):
        runner.run_one(**kwargs)

    assert signals == [(424247, signal.SIGTERM)]
    assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()


def test_per_instance_main_interrupt_cancels_futures_and_terminates_registry(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text("[]", encoding="utf-8")
    predictions.write_text("", encoding="utf-8")
    signals = []
    pools = []

    class ActiveProcess:
        pid = 424248

        def wait(self, timeout=None):
            return 0

    class FakeFuture:
        cancelled = False

        def cancel(self):
            self.cancelled = True
            return True

    class FakePool:
        def __init__(self, max_workers):
            self.future = FakeFuture()
            self.shutdown_calls = []
            pools.append(self)

        def submit(self, fn, **kwargs):
            kwargs["active_processes"].add(ActiveProcess())
            return self.future

        def shutdown(self, *, wait, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr(runner, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(
        runner,
        "load_eval_queue",
        lambda *args, **kwargs: [
            (
                "task-1",
                "model",
                {
                    "instance_id": "task-1",
                    "record_id": "r1",
                    "patch_sha256": "a" * 64,
                },
                {"instance_id": "task-1", "model_patch": "+fix"},
            )
        ],
    )
    monkeypatch.setattr(
        runner,
        "as_completed",
        lambda futures: (_ for _ in ()).throw(KeyboardInterrupt("main interrupted")),
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
            "--work-dir",
            str(tmp_path / "eval"),
            "--run-id",
            "run",
        ],
    )

    with pytest.raises(KeyboardInterrupt, match="main interrupted"):
        runner.main()

    assert signals == [(424248, signal.SIGTERM)]
    assert pools[0].future.cancelled is True
    assert pools[0].shutdown_calls == [(False, True)]


def test_per_instance_stop_race_after_popen_self_terminates(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    stop_event = threading.Event()
    active_processes = runner.ActiveProcessRegistry()
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    kwargs.update(
        active_processes=active_processes,
        stop_event=stop_event,
    )
    popen_entered = threading.Event()
    release_popen = threading.Event()
    signals = []
    results = []

    class CooperativeProcess:
        pid = 424249

        def wait(self, timeout=None):
            return 0

    def gated_popen(*args, **kwargs):
        popen_entered.set()
        assert release_popen.wait(timeout=2)
        return CooperativeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", gated_popen)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    worker = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    worker.start()
    assert popen_entered.wait(timeout=2)
    stop_event.set()
    assert active_processes.terminate_all(io.StringIO()) is True
    release_popen.set()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert results == [("task-1", 130)]
    assert signals == [(424249, signal.SIGTERM)]


def test_per_instance_main_cleanup_failure_retains_residual_claim(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    process = _spawn_term_ignoring_descendant(tmp_path)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    stop_event = threading.Event()
    active_processes = runner.ActiveProcessRegistry()
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    kwargs.update(
        active_processes=active_processes,
        stop_event=stop_event,
    )
    results = []

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        runner,
        "process_start_identity",
        lambda pid: f"start-{pid}",
    )

    def incomplete_registry_cleanup(active_process, log_file):
        del log_file
        os.killpg(active_process.pid, signal.SIGTERM)
        return False

    monkeypatch.setattr(
        runner,
        "terminate_process_group",
        incomplete_registry_cleanup,
    )
    worker = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    worker.start()
    claim_path = runner._claim_path(kwargs["work_dir"], "task-1")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            claim = {}
        if claim.get("status") == "running":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("worker did not publish running claim")

    try:
        stop_event.set()
        assert active_processes.terminate_all(io.StringIO()) is False
        worker.join(timeout=2)
        retained = json.loads(claim_path.read_text(encoding="utf-8"))

        assert worker.is_alive() is False
        assert results == [
            ("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)
        ]
        assert retained["status"] == "cleanup_failed"
        assert retained["evaluator_pgid"] == process.pid
        assert retained["lease_until_ns"] > time.time_ns()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_per_instance_main_continues_after_future_exception(monkeypatch, tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    seen = []
    predictions = []
    for task in ("task-1", "task-2"):
        prediction = {
            "instance_id": task,
            "record_id": f"{task}-r1",
            "model_name_or_path": "model",
            "model_patch": _patch(f"+{task}\n"),
        }
        predictions.append((task, "model", runner.prediction_identity(prediction), prediction))

    monkeypatch.setattr(runner, "load_eval_queue", lambda *args, **kwargs: predictions)

    def fake_run_one(**kwargs):
        seen.append(kwargs["iid"])
        if kwargs["iid"] == "task-1":
            raise RuntimeError("unexpected worker crash")
        return kwargs["iid"], 0

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(tmp_path / "dataset.json"),
            "--predictions",
            str(tmp_path / "predictions.jsonl"),
            "--work-dir",
            str(tmp_path / "eval"),
            "--run-id",
            "run",
            "--workers",
            "2",
        ],
    )

    assert runner.main() == 1
    assert sorted(seen) == ["task-1", "task-2"]


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


@pytest.mark.parametrize(
    "bad_run",
    [
        "not-an-object",
        {"base_run_dir": "", "tasks": []},
        {"base_run_dir": "relative/run", "tasks": []},
        {"base_run_dir": "/definitely/missing/opencollab-run", "tasks": []},
        {"base_run_dir": "/tmp", "side_name": "../escape", "tasks": []},
        {"base_run_dir": "/tmp", "tasks": "task-1"},
        {"base_run_dir": "/tmp", "tasks": ["task-1", 2]},
    ],
)
def test_wave_watchdog_bad_run_schema_is_incomplete_and_exit_two(tmp_path, bad_run):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text(json.dumps([bad_run]), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    summary = json.loads(result.stdout)
    assert summary["complete"] is False
    assert summary["input_errors"]
    assert summary["totals"]["invalid_runs"] == 1


def test_wave_watchdog_rejects_symlinked_base_run_directory(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    real = tmp_path / "real-run"
    real.mkdir()
    linked = tmp_path / "linked-run"
    linked.symlink_to(real, target_is_directory=True)
    config = tmp_path / "runs.json"
    config.write_text(
        json.dumps([{"base_run_dir": str(linked), "tasks": []}]),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    summary = json.loads(result.stdout)
    assert summary["complete"] is False
    assert summary["runs"][0]["config_errors"]


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_wave_watchdog_rejects_unsafe_runs_config_without_blocking(tmp_path, kind):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    if kind == "fifo":
        os.mkfifo(config)
    else:
        real = tmp_path / "real.json"
        real.write_text("[]", encoding="utf-8")
        config.symlink_to(real)

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(config)],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert "bounded regular JSON file" in result.stderr


def test_wave_watchdog_rejects_symlinked_runs_config_ancestor(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "runs.json").write_text("[]", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(script), "--runs-config", str(linked / "runs.json")],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    assert "bounded regular JSON file" in result.stderr


@pytest.mark.parametrize("flag", ["--json-output", "--markdown-output"])
@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_wave_watchdog_rejects_unsafe_output_without_blocking(
    tmp_path,
    flag,
    kind,
):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text("[]", encoding="utf-8")
    output = tmp_path / "summary.out"
    real = None
    if kind == "fifo":
        os.mkfifo(output)
    else:
        real = tmp_path / "real.out"
        real.write_text("original", encoding="utf-8")
        output.symlink_to(real)

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runs-config",
            str(config),
            flag,
            str(output),
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert "output path is not a regular file" in result.stderr
    if real is not None:
        assert real.read_text(encoding="utf-8") == "original"


def test_wave_watchdog_rejects_symlinked_output_parent(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_v3_wave_watchdog.py"
    config = tmp_path / "runs.json"
    config.write_text("[]", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runs-config",
            str(config),
            "--json-output",
            str(linked / "summary.json"),
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert list(outside.iterdir()) == []


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


def test_standard_report_without_sha_pairs_with_current_attempt_sidecar(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto"
    report_dir = side_dir / "reports" / "task-1"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    report_dir.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
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
    started_at_ns = 10_000
    (attempt_dir / "current.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": started_at_ns,
                "status": "started",
            }
        ),
        encoding="utf-8",
    )
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps({"task-1": {"resolved": True}}), encoding="utf-8")
    os.utime(report_path, ns=(started_at_ns + 1, started_at_ns + 1))

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "eval_done"
    assert row["eval"]["resolved_count"] == 1


def test_standard_stale_report_before_current_attempt_is_ignored(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto"
    report_dir = side_dir / "reports" / "task-1"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    report_dir.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r2",
        "model_patch": _patch("+new-candidate\n"),
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "r2",
        "patch_sha256": row_patch_sha(prediction),
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps({"task-1": {"resolved": True}}), encoding="utf-8")
    os.utime(report_path, ns=(10_000, 10_000))
    (attempt_dir / "current.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r2",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": 20_000,
                "status": "started",
            }
        ),
        encoding="utf-8",
    )

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["done_count"] == 0


def test_build_snapshots_reads_nested_direct_eval_technical_failure(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto" / "reports" / "task-1"
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
    (side_dir / "report.json").write_text(
        json.dumps(
            {
                "task-1": {
                    "schema": "opencollab.prolite_direct_eval.v1",
                    "status": "technical_eval_failed",
                    "resolved": False,
                    "error": True,
                    "patch_sha256": row_patch_sha(prediction),
                }
            }
        ),
        encoding="utf-8",
    )

    snapshots = build_snapshots(run_dir)

    row = task_status_row(snapshots[0])
    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["failed_count"] == 1
    assert row["eval"]["done_count"] == 0


def test_build_snapshots_reads_empty_eval_patch_invalid_as_failure(tmp_path):
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
                "status": "empty_eval_patch_invalid",
            }
        ),
        encoding="utf-8",
    )

    snapshots = build_snapshots(run_dir)

    row = task_status_row(snapshots[0])
    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["failed_count"] == 1
    assert row["eval"]["done_count"] == 0


def test_build_snapshots_prefers_newer_matching_eval_report(tmp_path):
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
    old_report = side_dir / "old-summary.json"
    old_report.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "patch_sha256": row_patch_sha(prediction),
                "status": "done",
                "unresolved_instances": 1,
            }
        ),
        encoding="utf-8",
    )
    new_report = side_dir / "redis-classified.json"
    new_report.write_text(
        json.dumps(
            {
                "task-1": {
                    "status": "technical_eval_failed",
                    "error": True,
                    "patch_sha256": row_patch_sha(prediction),
                }
            }
        ),
        encoding="utf-8",
    )
    os.utime(old_report, ns=(1, 1))
    os.utime(new_report, ns=(2, 2))

    snapshots = build_snapshots(run_dir)

    row = task_status_row(snapshots[0])
    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["done_count"] == 0
    assert row["eval"]["failed_count"] == 1
    assert row["eval"]["report_paths"] == [str(new_report)]


def test_discovery_reads_every_task_from_batch_report(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "task-1": {"resolved": True, "patch_sha256": "a" * 64},
                "task-2": {"resolved": False, "patch_sha256": "b" * 64},
            }
        ),
        encoding="utf-8",
    )

    reports = discover_eval_reports(tmp_path)

    assert [(item.task_id, item.resolved_count, item.unresolved_count) for item in reports] == [
        ("task-1", 1, 0),
        ("task-2", 0, 1),
    ]


def test_discovery_rejects_symlinked_report(tmp_path):
    outside = tmp_path / "outside-report"
    outside.mkdir()
    target = outside / "actual.json"
    target.write_text(
        json.dumps({"task-1": {"resolved": True, "patch_sha256": "a" * 64}}),
        encoding="utf-8",
    )
    side_dir = tmp_path / "reports"
    side_dir.mkdir()
    (side_dir / "report.json").symlink_to(target)

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="symlink"):
        discover_eval_reports(side_dir)


def test_discovery_rejects_symlinked_root_directory(tmp_path):
    actual = tmp_path / "actual-reports"
    actual.mkdir()
    (actual / "report.json").write_text(
        json.dumps({"task-1": {"resolved": True, "patch_sha256": "a" * 64}}),
        encoding="utf-8",
    )
    link = tmp_path / "linked-reports"
    link.symlink_to(actual, target_is_directory=True)

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="real directory"):
        discover_eval_reports(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_discovery_rejects_fifo_report_without_blocking(tmp_path):
    path = tmp_path / "report.json"
    os.mkfifo(path)

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="regular file"):
        discover_eval_reports(tmp_path)


def test_discovery_rejects_oversized_report(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery_mod, "MAX_DISCOVERY_JSON_FILE_BYTES", 64)
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "task-1": {
                    "resolved": True,
                    "patch_sha256": "a" * 64,
                    "padding": "x" * 200,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="file byte limit"):
        discover_eval_reports(tmp_path)


def test_discovery_rejects_excess_json_file_count(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery_mod, "MAX_DISCOVERY_JSON_FILES", 2)
    for index in range(3):
        (tmp_path / f"report-{index}.json").write_text(
            json.dumps(
                {
                    f"task-{index}": {
                        "resolved": True,
                        "patch_sha256": f"{index}" * 64,
                    }
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="JSON files"):
        discover_eval_reports(tmp_path)


def test_discovery_rejects_excess_aggregate_json_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery_mod, "MAX_DISCOVERY_JSON_TOTAL_BYTES", 100)
    for index in range(2):
        (tmp_path / f"report-{index}.json").write_text(
            json.dumps(
                {
                    f"task-{index}": {
                        "resolved": True,
                        "patch_sha256": f"{index}" * 64,
                    }
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="total byte limit"):
        discover_eval_reports(tmp_path)


def test_discovery_non_json_entry_overflow_becomes_technical_failure(
    monkeypatch,
    tmp_path,
):
    run_dir = tmp_path / "run"
    _write_ready_eval_pair(run_dir)
    side_dir = run_dir / "official_eval_auto"
    side_dir.mkdir()
    monkeypatch.setattr(discovery_mod, "MAX_DISCOVERY_ENTRIES", 2)
    for index in range(3):
        (side_dir / f"noise-{index}.txt").write_text("noise", encoding="utf-8")

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["ready_for_eval"] is False
    assert row["eval"]["failed_count"] == 1
    assert "directory entries" in row["eval"]["report_paths"][0]


def test_discovery_depth_overflow_becomes_technical_failure(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    _write_ready_eval_pair(run_dir)
    side_dir = run_dir / "official_eval_auto"
    side_dir.mkdir()
    monkeypatch.setattr(discovery_mod, "MAX_DISCOVERY_DEPTH", 1)
    (side_dir / "level-1" / "level-2").mkdir(parents=True)

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["failed_count"] == 1
    assert "exceeds depth" in row["eval"]["report_paths"][0]


def test_discovery_scandir_error_becomes_technical_failure(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    _write_ready_eval_pair(run_dir)
    (run_dir / "official_eval_auto").mkdir()

    def fail_scandir(_fd):
        raise PermissionError("denied")

    monkeypatch.setattr(discovery_mod.os, "scandir", fail_scandir)

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["failed_count"] == 1
    assert "cannot scan" in row["eval"]["report_paths"][0]


def test_discovery_rejects_nested_symlink_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    side_dir = tmp_path / "reports"
    side_dir.mkdir()
    (side_dir / "linked-dir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(discovery_mod.EvalArtifactDiscoveryError, match="symlink"):
        discover_eval_reports(side_dir)


def test_discovery_rejects_symlinked_attempt_owner(tmp_path):
    run_dir = tmp_path / "run"
    side_dir = run_dir / "official_eval_auto"
    report_dir = side_dir / "reports" / "task-1"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    report_dir.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
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
    (report_dir / "report.json").write_text(
        json.dumps({"task-1": {"resolved": True}}),
        encoding="utf-8",
    )
    owner = tmp_path / "attempt-owner.json"
    owner.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": row_patch_sha(prediction),
                "started_at_ns": 1,
                "status": "started",
            }
        ),
        encoding="utf-8",
    )
    (attempt_dir / "r1.json").symlink_to(owner)

    row = task_status_row(build_snapshots(run_dir)[0])

    assert row["state"] == "technical_eval_failed"
    assert row["eval"]["done_count"] == 0
    assert row["eval"]["failed_count"] == 1


def test_discovery_ignores_summary_with_invalid_count_type(tmp_path):
    (tmp_path / "invalid.json").write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "status": "done",
                "resolved_instances": {"unexpected": "mapping"},
            }
        ),
        encoding="utf-8",
    )

    assert discover_eval_reports(tmp_path) == []


def test_discovery_uses_top_level_resolved_boolean_for_summary(tmp_path):
    for task, resolved in (("task-1", True), ("task-2", False)):
        (tmp_path / f"{task}.json").write_text(
            json.dumps(
                {
                    "task": task,
                    "status": "done",
                    "resolved": resolved,
                    "patch_sha256": ("a" if resolved else "b") * 64,
                }
            ),
            encoding="utf-8",
        )

    reports = discover_eval_reports(tmp_path)

    assert [(item.task_id, item.resolved_count, item.unresolved_count) for item in reports] == [
        ("task-1", 1, 0),
        ("task-2", 0, 1),
    ]


def test_official_report_with_string_error_is_technical_failure(tmp_path):
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "task-1": {
                    "resolved": False,
                    "error": "docker daemon unavailable",
                    "patch_sha256": "a" * 64,
                }
            }
        ),
        encoding="utf-8",
    )

    reports = discover_eval_reports(tmp_path)

    assert reports[0].status == "technical_eval_failed"


def test_top_level_report_with_string_error_is_technical_failure(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "task": "task-1",
                "error": "docker daemon unavailable",
                "patch_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    reports = discover_eval_reports(tmp_path)

    assert reports[0].status == "technical_eval_failed"


def test_auto_eval_fingerprints_top_level_task_report_before_new_attempt(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    report_path = tmp_path / "summary.json"
    report_path.write_text(
        json.dumps({"task": "task-1", "resolved": True}),
        encoding="utf-8",
    )
    prior_reports = driver._report_fingerprints(tmp_path, "task-1")
    (tmp_path / "attempt.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "new-record",
                "patch_sha256": "b" * 64,
                "started_at_ns": time.time_ns(),
                "status": "started",
                "pid": 0,
                "prior_reports": prior_reports,
            }
        ),
        encoding="utf-8",
    )

    reports = discover_eval_reports(tmp_path)

    assert "summary.json" in prior_reports
    assert reports[0].patch_sha == ""
    assert reports[0].record_id == ""


def test_per_instance_release_does_not_delete_successor_claim(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    acquired, claim_path = runner.acquire_claim(
        tmp_path,
        "task-1",
        identity,
        lease_seconds=30,
        owner_token="owner-1",
    )
    assert acquired is True
    successor = {
        "schema": "opencollab.swe_eval_claim.v1",
        **identity,
        "owner_token": "owner-2",
        "pid": os.getpid(),
    }
    runner._write_json_atomic(claim_path, successor)

    released = runner.release_claim(claim_path, owner_token="owner-1")

    assert released is False
    assert json.loads(claim_path.read_text(encoding="utf-8"))["owner_token"] == "owner-2"


def test_per_instance_expired_claim_rejects_live_residual_group(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    work_dir = tmp_path / "eval"
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    claim_path = runner._claim_path(work_dir, "task-1")
    try:
        runner._write_json_atomic(
            claim_path,
            {
                "schema": "opencollab.swe_eval_claim.v1",
                **identity,
                "owner_token": "old-owner",
                "status": "cleanup_failed",
                "lease_until_ns": 1,
                "evaluator_pgid": process.pid,
                "evaluator_start_identity": runner.process_start_identity(process.pid),
            },
        )

        acquired, returned_path = runner.acquire_claim(
            work_dir,
            "task-1",
            identity,
            lease_seconds=10,
            owner_token="new-owner",
        )
        retained = json.loads(claim_path.read_text(encoding="utf-8"))

        assert acquired is False
        assert returned_path == claim_path
        assert retained["owner_token"] == "old-owner"
        assert retained["lease_until_ns"] > time.time_ns()
        assert retained["residual_checked_at_ns"] > 0
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_docker_eval_wrapper_accepts_positive_fractional_timeout(monkeypatch):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_API_TIMEOUT", "2.5")
    monkeypatch.setattr(
        wrapper,
        "_original_from_env",
        lambda *args, **kwargs: captured.update(kwargs) or "client",
    )

    result = wrapper._from_env_with_timeout(version="auto")

    assert result == "client"
    assert captured == {"version": "auto", "timeout": 2.5}


def test_docker_eval_wrapper_blank_primary_falls_back_to_client_timeout(monkeypatch):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_API_TIMEOUT", " ")
    monkeypatch.setenv("DOCKER_CLIENT_TIMEOUT", "3")
    monkeypatch.setattr(
        wrapper,
        "_original_from_env",
        lambda *args, **kwargs: captured.update(kwargs) or "client",
    )

    wrapper._from_env_with_timeout()

    assert captured["timeout"] == 3.0


@pytest.mark.parametrize("value", ["bad", "0", "-1", "nan", "inf"])
def test_docker_eval_timeout_rejects_non_positive_or_non_finite(value):
    wrapper = importlib.import_module("scripts.run_swebench_eval_with_docker_timeout")
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")

    with pytest.raises(ValueError, match="positive finite number"):
        wrapper.positive_timeout_seconds(value, name="TIMEOUT")
    with pytest.raises(ValueError, match="positive finite number"):
        runner.positive_timeout_seconds(value, name="TIMEOUT")


def test_smoke_batch_returns_failure_when_generator_fails(monkeypatch, tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.delenv("DOCKER_DEFAULT_PLATFORM", raising=False)
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir()
    (instances_dir / "task.json").write_text(
        json.dumps({"instance_id": "task-1"}),
        encoding="utf-8",
    )
    observed = {}
    def fake_make_test_spec(instance, namespace, arch):
        observed["arch"] = arch
        return SimpleNamespace(instance_image_key="image")

    monkeypatch.setattr(driver, "make_test_spec", fake_make_test_spec)

    def fake_run(*args, **kwargs):
        observed["platform"] = kwargs["env"]["DOCKER_DEFAULT_PLATFORM"]
        return 9, "failed"

    monkeypatch.setattr(driver, "_run_generator", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_smoke_batch.py",
            "--instances-dir",
            str(instances_dir),
            "--output-dir",
            str(tmp_path / "output"),
            "--arch",
            "arm64",
        ],
    )

    assert driver.main() == 1
    assert observed == {"arch": "arm64", "platform": "linux/arm64"}


def test_smoke_generator_outer_timeout_kills_term_ignoring_descendant(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(started)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not p.exists()]; "
        "time.sleep(10)"
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=0.3,
        spawn_timeout=0.5,
        term_timeout=0.1,
        kill_timeout=0.5,
    )

    assert returncode in {124, driver.TECHNICAL_EXIT_CODE}
    assert "timeout" in reason or "timed out" in reason
    assert started.exists()
    time.sleep(0.9)
    assert not finished.exists()


def test_smoke_generator_normal_exit_kills_residual_descendant(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    ready = tmp_path / "normal-exit-child.ready"
    finished = tmp_path / "normal-exit-child.finished"
    child_code = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "[(time.sleep(0.01)) for _ in range(200) if not p.exists()]"
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", leader_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=2,
        spawn_timeout=0.5,
        term_timeout=0.05,
        kill_timeout=1.0,
    )

    assert returncode == 0
    assert reason == ""
    assert ready.exists()
    time.sleep(0.9)
    assert not finished.exists()


def test_smoke_generator_normal_exit_cleanup_failure_is_technical(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(
        driver,
        "_ensure_process_tree_quiesced_after_wait",
        lambda *args, **kwargs: False,
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", "raise SystemExit(0)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=2,
        spawn_timeout=0.5,
        term_timeout=0.05,
        kill_timeout=0.5,
    )

    assert returncode == driver.TECHNICAL_EXIT_CODE
    assert "leader exited" in reason


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_smoke_generator_signal_cleans_child_before_parent_exits(tmp_path, signum):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    generator_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(started)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not p.exists()]; "
        "time.sleep(10)"
    )
    script = f"""
import os, sys
from pathlib import Path
from scripts.run_swebench_smoke_batch import _run_generator
_run_generator(
    [sys.executable, "-c", {generator_code!r}],
    cwd=Path({str(tmp_path)!r}),
    env=os.environ.copy(),
    outer_timeout=10,
    spawn_timeout=1,
    term_timeout=0.1,
    kill_timeout=0.5,
)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    parent = subprocess.Popen([sys.executable, "-c", script], env=env)
    for _ in range(300):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()

    parent.send_signal(signum)
    parent.wait(timeout=5)

    time.sleep(0.9)
    assert not finished.exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(91)])
def test_smoke_generator_interruption_immediately_after_worker_start_cleans(
    monkeypatch,
    tmp_path,
    interruption,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    real_wait = driver._wait_event_resisting_interrupt
    calls = 0

    def interrupt_first_wait(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise interruption
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(driver, "_wait_event_resisting_interrupt", interrupt_first_wait)

    with pytest.raises(type(interruption)):
        driver._run_generator(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            outer_timeout=10,
            spawn_timeout=1,
            term_timeout=0.1,
            kill_timeout=0.5,
        )

    assert calls >= 2


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "inf"),
        ("--timeout", "0"),
        ("--outer-timeout", "nan"),
        ("--spawn-timeout", "-1"),
    ],
)
def test_smoke_batch_rejects_invalid_timeout_before_io(
    monkeypatch,
    tmp_path,
    flag,
    value,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_smoke_batch.py",
            "--instances-dir",
            str(tmp_path / "missing"),
            "--output-dir",
            str(tmp_path / "output"),
            flag,
            value,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        driver.main()

    assert exc_info.value.code == 2


def test_smoke_batch_patch_reader_rejects_truncated_tail(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+fixed"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "current",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "current",
                    "patch_sha256": patch_sha,
                    "workflow_status": "done",
                    "runner_returncode": 0,
                },
            }
        )
        + "\n"
        + '{"instance_id":',
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="invalid JSONL"):
        driver._prediction_has_patch(output, "task-1")


def test_smoke_batch_patch_reader_rejects_non_object_json_rows(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+fixed"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        "[]\nnull\n"
        + json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "current",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "current",
                    "patch_sha256": patch_sha,
                    "workflow_status": "done",
                    "runner_returncode": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="must be an object"):
        driver._prediction_has_patch(output, "task-1")


def test_smoke_batch_patch_reader_uses_latest_candidate(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+old"})
        + "\n"
        + json.dumps({"instance_id": "task-1", "model_patch": ""})
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_reruns_legacy_patch_without_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+legacy"}) + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_rejects_failed_embedded_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "failed-record",
                "model_patch": "+partial",
                "workflow_metric": {
                    "record_id": "failed-record",
                    "workflow_status": "error",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_accepts_timeout_patch_metric(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+partial"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "timeout-record",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "timeout-record",
                    "patch_sha256": patch_sha,
                    "workflow_status": "done_with_timeout_patch",
                    "runner_returncode": 124,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is True


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("done", 1), ("done_with_timeout_patch", 1), ("done_with_timeout_patch", 0)],
)
def test_smoke_batch_patch_reader_reruns_status_returncode_mismatch(
    tmp_path,
    status,
    returncode,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    patch = "+partial"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "mismatch-record",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "mismatch-record",
                    "patch_sha256": patch_sha,
                    "workflow_status": status,
                    "runner_returncode": returncode,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_smoke_batch_patch_reader_rejects_metric_for_different_patch(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "record_id": "mismatch",
                "model_patch": "+current",
                "workflow_metric": {
                    "instance_id": "task-1",
                    "record_id": "mismatch",
                    "patch_sha256": "0" * 64,
                    "workflow_status": "done",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert driver._prediction_has_patch(output, "task-1") is False


def test_per_instance_dataset_rejects_symlink(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"instance_id": "task-1"}), encoding="utf-8")
    link = tmp_path / "dataset.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="bounded regular file"):
        runner.read_dataset(link)


def test_per_instance_dataset_and_predictions_reject_symlinked_ancestor(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dataset.json").write_text("[]", encoding="utf-8")
    (outside / "predictions.jsonl").write_text("", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="bounded regular file"):
        runner.read_dataset(linked / "dataset.json")
    with pytest.raises(records_mod.UnsafeRecordInputError):
        runner.read_jsonl(linked / "predictions.jsonl")


def test_per_instance_prediction_growth_during_read_is_rejected(
    tmp_path,
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({"instance_id": "task-1", "model_patch": "+one"}) + "\n",
        encoding="utf-8",
    )
    original_read = runner.os.read
    mutated = False

    def mutate_after_read(fd, size):
        nonlocal mutated
        chunk = original_read(fd, size)
        if chunk and not mutated:
            mutated = True
            with predictions.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"instance_id": "task-2", "model_patch": "+two"})
                    + "\n"
                )
        return chunk

    monkeypatch.setattr(runner.os, "read", mutate_after_read)

    with pytest.raises(records_mod.UnsafeRecordInputError):
        runner.read_jsonl(predictions)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_per_instance_dataset_rejects_fifo_without_blocking(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    path = tmp_path / "dataset.jsonl"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="bounded regular file"):
        runner.read_dataset(path)


def test_per_instance_dataset_rejects_oversized_file(tmp_path, monkeypatch):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    monkeypatch.setattr(runner, "MAX_DATASET_BYTES", 32)
    path = tmp_path / "dataset.jsonl"
    path.write_bytes(b'{"instance_id":"' + b"x" * 64 + b'"}\n')

    with pytest.raises(ValueError, match="dataset exceeds 32 bytes"):
        runner.read_dataset(path)


def test_per_instance_report_done_uses_single_opened_stat_snapshot(
    tmp_path,
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"task-1": {"resolved": True}}),
        encoding="utf-8",
    )
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    prior_fingerprint = runner.file_fingerprint(report)
    runner.write_identity(
        runner.identity_path(report),
        identity,
        status="started",
        prior_report_fingerprint=prior_fingerprint,
    )
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    original_read = runner._read_bounded_json_safe
    swapped = False

    def replace_after_open(path, *args, **kwargs):
        nonlocal swapped
        document = original_read(path, *args, **kwargs)
        if path == report and document is not None and not swapped:
            swapped = True
            report.unlink()
            report.symlink_to(victim)
        return document

    monkeypatch.setattr(runner, "_read_bounded_json_safe", replace_after_open)

    assert runner.report_is_done(report, "task-1", identity) is False
    assert report.is_symlink()
    assert victim.read_text(encoding="utf-8") == "{}"


def test_per_instance_report_without_boolean_outcome_is_never_done(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    report = tmp_path / "report.json"
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    report.write_text(
        json.dumps(
            {
                "task-1": {
                    "status": "done",
                    "patch_sha256": identity["patch_sha256"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert runner.report_is_done(report, "task-1", identity) is False


def test_per_instance_candidate_write_reports_file_fsync_failure(tmp_path, monkeypatch):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    prediction = {"instance_id": "task-1", "model_patch": "+fixed"}
    identity = runner.prediction_identity(prediction)

    def fail_fsync(_fd):
        raise OSError("candidate fsync failed")

    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="candidate fsync failed"):
        runner.write_candidate_prediction(tmp_path, prediction, identity)

    candidate = runner.candidate_predictions_path(tmp_path, identity)
    assert not candidate.exists()
    assert list(candidate.parent.glob(f".{candidate.name}.*.tmp")) == []


def test_per_instance_candidate_and_claim_reject_symlinked_state_parent(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work_dir / ".opencollab").symlink_to(outside, target_is_directory=True)
    prediction = {
        "instance_id": "task-1",
        "model_name_or_path": "model",
        "model_patch": "+fixed",
    }
    identity = runner.prediction_identity(prediction)

    with pytest.raises(OSError):
        runner.write_candidate_prediction(work_dir, prediction, identity)
    with pytest.raises(OSError):
        runner.acquire_claim(
            work_dir,
            "task-1",
            identity,
            lease_seconds=30,
            owner_token="owner",
        )

    assert list(outside.iterdir()) == []


def test_per_instance_report_does_not_follow_symlinked_ancestor(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    report = outside / "run" / "model" / "task-1" / "report.json"
    report.parent.mkdir(parents=True)
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    report.write_text(
        json.dumps(
            {
                "task-1": {
                    "resolved": True,
                    "patch_sha256": identity["patch_sha256"],
                }
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "logs").symlink_to(outside, target_is_directory=True)
    linked_report = (
        work_dir
        / "logs"
        / "run"
        / "model"
        / "task-1"
        / "report.json"
    )

    assert runner.report_is_done(linked_report, "task-1", identity) is False


def test_per_instance_main_rejects_symlinked_work_dir(tmp_path, monkeypatch):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset = tmp_path / "dataset.json"
    predictions = tmp_path / "predictions.jsonl"
    dataset.write_text("[]", encoding="utf-8")
    predictions.write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_eval_per_instance.py",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
            "--work-dir",
            str(linked / "eval"),
            "--run-id",
            "run",
        ],
    )

    with pytest.raises(SystemExit) as captured:
        runner.main()

    assert captured.value.code == 2
    assert list(outside.iterdir()) == []


def test_per_instance_claim_lock_rejects_symlink(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    claim = runner._claim_path(work_dir, "task-1")
    claim.parent.mkdir(parents=True)
    victim = tmp_path / "victim.lock"
    victim.write_text("unchanged", encoding="utf-8")
    claim.with_suffix(".lock").symlink_to(victim)

    with pytest.raises(OSError, match="non-regular"):
        runner.acquire_claim(
            work_dir,
            "task-1",
            {"instance_id": "task-1"},
            lease_seconds=30,
            owner_token="owner",
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_per_instance_claim_lock_has_bounded_wait(tmp_path, monkeypatch):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    claim = runner._claim_path(work_dir, "task-1")
    claim.parent.mkdir(parents=True)
    lock_path = claim.with_suffix(".lock")
    lock_path.touch()
    holder = os.open(lock_path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(runner, "HARNESS_LOCK_TIMEOUT_SECONDS", 0.03)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring claim lock"):
            runner.acquire_claim(
                work_dir,
                "task-1",
                {"instance_id": "task-1"},
                lease_seconds=30,
                owner_token="owner",
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_per_instance_concurrent_first_claim_lock_creation_retries(
    tmp_path,
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    work_dir = tmp_path / "eval"
    claim = runner._claim_path(work_dir, "task-1")
    lock_path = claim.with_suffix(".lock")
    original_lstat = runner.Path.lstat
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    missing_observations = 0

    def synchronized_missing_lstat(path):
        nonlocal missing_observations
        synchronize = False
        if path == lock_path:
            with counter_lock:
                if missing_observations < 2:
                    missing_observations += 1
                    synchronize = True
        if synchronize:
            barrier.wait(timeout=2)
            raise FileNotFoundError(lock_path)
        return original_lstat(path)

    monkeypatch.setattr(runner.Path, "lstat", synchronized_missing_lstat)
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    results = []
    errors = []

    def acquire(owner):
        try:
            results.append(
                runner.acquire_claim(
                    work_dir,
                    "task-1",
                    identity,
                    lease_seconds=30,
                    owner_token=owner,
                )[0]
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(f"owner-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert sorted(results) == [False, True]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_auto_eval_claim_lock_rejects_fifo_without_blocking(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    os.mkfifo(claim.with_name("claim.json.lock"))

    with pytest.raises(OSError, match="non-regular"):
        driver._acquire_claim(claim, {"pid": os.getpid()})


def test_auto_eval_claim_lock_rejects_symlink(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    victim = tmp_path / "victim.lock"
    victim.write_text("unchanged", encoding="utf-8")
    claim.with_name("claim.json.lock").symlink_to(victim)

    with pytest.raises(OSError, match="non-regular"):
        driver._acquire_claim(claim, {"pid": os.getpid()})

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_auto_eval_claim_lock_has_bounded_wait(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    lock_path = claim.with_name("claim.json.lock")
    lock_path.touch()
    holder = os.open(lock_path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(driver, "HARNESS_LOCK_TIMEOUT_SECONDS", 0.03)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring claim lock"):
            driver._acquire_claim(claim, {"pid": os.getpid()})
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_auto_eval_concurrent_first_claim_lock_creation_retries(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    lock_path = claim.with_name("claim.json.lock")
    original_lstat = driver.Path.lstat
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    missing_observations = 0

    def synchronized_missing_lstat(path):
        nonlocal missing_observations
        synchronize = False
        if path == lock_path:
            with counter_lock:
                if missing_observations < 2:
                    missing_observations += 1
                    synchronize = True
        if synchronize:
            barrier.wait(timeout=2)
            raise FileNotFoundError(lock_path)
        return original_lstat(path)

    monkeypatch.setattr(driver.Path, "lstat", synchronized_missing_lstat)
    results = []
    errors = []

    def acquire(owner):
        try:
            results.append(
                driver._acquire_claim(
                    claim,
                    {"pid": os.getpid(), "owner": owner},
                )[0]
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(f"owner-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert sorted(results) == [False, True]


def test_auto_eval_claim_path_rejects_side_name_traversal(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    args = SimpleNamespace(run_dir=tmp_path, side_name="../escape")

    with pytest.raises(ValueError, match="one non-dot path component"):
        driver._claim_path(args, "task-1")

    assert not (tmp_path.parent / "escape").exists()


def test_auto_eval_state_writes_reject_symlinked_opencollab_parent(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    side_dir = tmp_path / "side"
    outside = tmp_path / "outside"
    side_dir.mkdir()
    outside.mkdir()
    (side_dir / ".opencollab").symlink_to(outside, target_is_directory=True)
    claim = side_dir / ".opencollab" / "claims" / "claim.json"
    attempt = side_dir / ".opencollab" / "attempts" / "attempt.json"
    log = side_dir / ".opencollab" / "logs" / "attempt.log"

    with pytest.raises(OSError):
        driver._acquire_claim(claim, {"pid": os.getpid()})
    with pytest.raises(OSError):
        driver._write_json(attempt, {"status": "technical_eval_failed"})
    with pytest.raises(OSError):
        driver._open_append_binary(log)

    assert list(outside.iterdir()) == []


def test_auto_eval_wrapper_rejects_symlinked_state_parent(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    side_dir = tmp_path / "side"
    outside = tmp_path / "outside"
    side_dir.mkdir()
    outside.mkdir()
    (side_dir / ".opencollab").symlink_to(outside, target_is_directory=True)
    identity = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
        "started_at_ns": time.time_ns(),
        "status": "launching",
        "pid": 0,
    }
    claim = side_dir / ".opencollab" / "claims" / "claim.json"
    attempt = side_dir / ".opencollab" / "attempts" / "attempt.json"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            driver._EVAL_WRAPPER,
            str(claim),
            str(attempt),
            json.dumps({**identity, "schema": "opencollab.swe_eval_claim.v1"}),
            json.dumps(identity),
            json.dumps([sys.executable, "-c", "pass"]),
        ],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert list(outside.iterdir()) == []


def test_auto_eval_expired_unverified_owner_claim_is_recoverable(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    now_ns = time.time_ns()
    claim.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_claim.v1",
                "status": "started",
                "pid": os.getpid(),
                "owner_start_identity": "",
                "started_at_ns": now_ns - 120_000_000_000,
                "heartbeat_at_ns": now_ns - 120_000_000_000,
                "lease_expires_at_ns": now_ns - 90_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "_pid_is_active", lambda pid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda pid: "")
    replacement = {"schema": "opencollab.swe_eval_claim.v1", "pid": 0}

    acquired, existing = driver._acquire_claim(claim, replacement)

    assert acquired is True
    assert existing == replacement
    assert json.loads(claim.read_text(encoding="utf-8")) == replacement


def test_auto_eval_fresh_heartbeat_retains_unverified_live_owner(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    now_ns = time.time_ns()
    existing = {
        "schema": "opencollab.swe_eval_claim.v1",
        "status": "started",
        "pid": os.getpid(),
        "owner_start_identity": "",
        "started_at_ns": now_ns,
        "heartbeat_at_ns": now_ns,
        "lease_expires_at_ns": now_ns + 20_000_000_000,
    }
    claim.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(driver, "_pid_is_active", lambda pid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda pid: "")

    acquired, observed = driver._acquire_claim(claim, {"pid": 0})

    assert acquired is False
    assert observed == existing


def test_auto_eval_pid_reuse_mismatch_reclaims_claim_even_with_fresh_lease(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    now_ns = time.time_ns()
    claim.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_claim.v1",
                "status": "started",
                "pid": 424242,
                "owner_start_identity": "proc:old",
                "started_at_ns": now_ns,
                "heartbeat_at_ns": now_ns,
                "lease_expires_at_ns": now_ns + 20_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "_pid_is_active", lambda pid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda pid: "proc:new")
    replacement = {"schema": "opencollab.swe_eval_claim.v1", "pid": 0}

    acquired, observed = driver._acquire_claim(claim, replacement)

    assert acquired is True
    assert observed == replacement


def test_auto_eval_expired_unverified_residual_group_is_recoverable(
    monkeypatch,
    tmp_path,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    now_ns = time.time_ns()
    claim.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_claim.v1",
                "status": "technical_eval_failed",
                "pid": 0,
                "evaluator_pgid": 424243,
                "evaluator_start_identity": "",
                "started_at_ns": now_ns - 120_000_000_000,
                "heartbeat_at_ns": now_ns - 120_000_000_000,
                "lease_expires_at_ns": now_ns - 90_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "_process_group_exists", lambda pgid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda pid: "")
    replacement = {"schema": "opencollab.swe_eval_claim.v1", "pid": 0}

    acquired, observed = driver._acquire_claim(claim, replacement)

    assert acquired is True
    assert observed == replacement


def test_auto_eval_rejects_symlink_claim_without_touching_target(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    claim.symlink_to(victim)
    payload = {"schema": "opencollab.swe_eval_claim.v1", "pid": os.getpid()}

    with pytest.raises(OSError, match="bounded regular file"):
        driver._acquire_claim(claim, payload)

    assert claim.is_symlink()
    assert victim.read_text(encoding="utf-8") == json.dumps({"pid": os.getpid()})


def test_auto_eval_reclaims_recent_oversized_claim(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    monkeypatch.setattr(driver, "MAX_CLAIM_BYTES", 32)
    claim = tmp_path / "claim.json"
    claim.write_bytes(b"x" * 33)
    payload = {"schema": "opencollab.swe_eval_claim.v1", "pid": os.getpid()}

    acquired, existing = driver._acquire_claim(claim, payload)

    assert acquired is True
    assert existing == payload
    assert json.loads(claim.read_text(encoding="utf-8")) == payload


def test_auto_eval_reclaims_malformed_claim_with_future_mtime(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    claim.write_text("", encoding="utf-8")
    future = time.time() + 3600
    os.utime(claim, (future, future))
    payload = {"schema": "opencollab.swe_eval_claim.v1", "pid": os.getpid()}

    acquired, existing = driver._acquire_claim(claim, payload)

    assert acquired is True
    assert existing == payload


def test_auto_eval_report_fingerprint_rejects_symlink(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    side_dir = tmp_path / "side"
    side_dir.mkdir()
    victim = tmp_path / "report.json"
    victim.write_text(json.dumps({"instance_id": "task-1"}), encoding="utf-8")
    (side_dir / "linked.json").symlink_to(victim)

    assert driver._report_fingerprints(side_dir, "task-1") == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_auto_eval_report_fingerprint_rejects_fifo_without_blocking(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    side_dir = tmp_path / "side"
    side_dir.mkdir()
    os.mkfifo(side_dir / "report.json")

    assert driver._report_fingerprints(side_dir, "task-1") == {}


def test_auto_eval_report_fingerprint_rejects_symlink_root(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    real_side = tmp_path / "real-side"
    real_side.mkdir()
    side_link = tmp_path / "side"
    side_link.symlink_to(real_side, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        driver._report_fingerprints(side_link, "task-1")


def test_auto_eval_report_fingerprint_bounds_file_count(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    monkeypatch.setattr(driver, "MAX_REPORT_SCAN_FILES", 1)
    side_dir = tmp_path / "side"
    side_dir.mkdir()
    for index in range(2):
        (side_dir / f"report-{index}.json").write_text(
            json.dumps({"instance_id": "task-1"}),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="exceeds 1 JSON files"):
        driver._report_fingerprints(side_dir, "task-1")


def test_auto_eval_report_fingerprint_bounds_all_directory_entries(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    monkeypatch.setattr(driver, "MAX_REPORT_SCAN_ENTRIES", 2)
    side_dir = tmp_path / "side"
    side_dir.mkdir()
    for index in range(3):
        (side_dir / f"noise-{index}.txt").write_text("noise", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds 2 directory entries"):
        driver._report_fingerprints(side_dir, "task-1")


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


def test_auto_eval_markdown_write_rejects_symlink_without_touching_target(tmp_path):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    victim = tmp_path / "victim.md"
    victim.write_text("unchanged", encoding="utf-8")
    output = tmp_path / "status.md"
    output.symlink_to(victim)

    with pytest.raises(OSError, match="non-regular auto-eval destination"):
        driver._write_markdown(output, _auto_eval_summary())

    assert output.is_symlink()
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_auto_eval_markdown_write_reports_directory_fsync_failure(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")

    original_fsync = driver.os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("markdown directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(driver.os, "fsync", fail_directory_fsync)
    output = tmp_path / "status.md"
    with pytest.raises(OSError, match="markdown directory fsync failed"):
        driver._write_markdown(output, _auto_eval_summary())

    assert output.exists()
    assert list(tmp_path.glob(".status.md.*.tmp")) == []


def test_auto_eval_json_write_cleans_temp_after_file_fsync_failure(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")

    def fail_fsync(_fd):
        raise OSError("json file fsync failed")

    monkeypatch.setattr(driver.os, "fsync", fail_fsync)
    output = tmp_path / "status.json"
    with pytest.raises(OSError, match="json file fsync failed"):
        driver._write_json(output, {"status": "new"})

    assert not output.exists()
    assert list(tmp_path.glob(".status.json.*.tmp")) == []


def test_per_instance_atomic_write_preserves_primary_error_when_temp_unlink_fails(
    tmp_path,
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    original_unlink = runner.os.unlink

    def fail_fsync(_fd):
        raise OSError("primary file fsync failure")

    def fail_temp_unlink(path, *args, **kwargs):
        name = os.fspath(path)
        if name.startswith(".oc-") and name.endswith(".tmp"):
            raise OSError("secondary temp unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    monkeypatch.setattr(runner.os, "unlink", fail_temp_unlink)
    output = tmp_path / "record.json"

    with pytest.raises(OSError, match="primary file fsync failure") as captured:
        runner._write_json_atomic(output, {"status": "new"})

    assert any(
        "secondary temp unlink failure" in note
        for note in getattr(captured.value, "__notes__", [])
    )
    temporary = next(tmp_path.glob(".oc-*.tmp"))
    original_unlink(temporary)


def test_auto_eval_atomic_write_preserves_primary_error_when_temp_unlink_fails(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    original_unlink = driver.os.unlink

    def fail_fsync(_fd):
        raise OSError("primary file fsync failure")

    def fail_temp_unlink(path, *args, **kwargs):
        name = os.fspath(path)
        if name.startswith(".record.json.") and name.endswith(".tmp"):
            raise OSError("secondary temp unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(driver.os, "fsync", fail_fsync)
    monkeypatch.setattr(driver.os, "unlink", fail_temp_unlink)
    output = tmp_path / "record.json"

    with pytest.raises(OSError, match="primary file fsync failure") as captured:
        driver._write_json(output, {"status": "new"})

    assert any(
        "secondary temp unlink failure" in note
        for note in getattr(captured.value, "__notes__", [])
    )
    temporary = next(tmp_path.glob(".record.json.*.tmp"))
    original_unlink(temporary)


def test_smoke_instance_reader_rejects_symlink(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"instance_id": "task-1"}), encoding="utf-8")
    link = tmp_path / "instance.json"
    link.symlink_to(victim)

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        driver._read_instance(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_smoke_instance_reader_rejects_fifo_without_blocking(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    path = tmp_path / "instance.json"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        driver._read_instance(path)


def test_smoke_instance_reader_rejects_oversized_document(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(driver, "MAX_INSTANCE_BYTES", 32)
    path = tmp_path / "instance.json"
    path.write_bytes(b'{"instance_id":"' + b"x" * 64 + b'"}')

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        driver._read_instance(path)


def test_smoke_instance_discovery_rejects_symlink_root(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    real_dir = tmp_path / "real-instances"
    real_dir.mkdir()
    link = tmp_path / "instances"
    link.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        driver._discover_instance_paths(link, limit=1)


def test_smoke_instance_discovery_bounds_directory_entries(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    monkeypatch.setattr(driver, "MAX_INSTANCE_DIRECTORY_ENTRIES", 2)
    instances = tmp_path / "instances"
    instances.mkdir()
    for index in range(3):
        (instances / f"entry-{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds 2 entries"):
        driver._discover_instance_paths(instances, limit=1)


def test_smoke_manifest_rejects_symlink_without_touching_target(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    manifest = tmp_path / "manifest.jsonl"
    manifest.symlink_to(victim)

    with pytest.raises(OSError, match="non-regular"):
        driver._append_manifest_record(manifest, {"instance_id": "task-1"})

    assert victim.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_smoke_manifest_rejects_fifo_without_blocking(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    manifest = tmp_path / "manifest.jsonl"
    os.mkfifo(manifest)

    with pytest.raises(OSError, match="non-regular"):
        driver._append_manifest_record(manifest, {"instance_id": "task-1"})


def test_smoke_manifest_lock_has_bounded_wait(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    manifest = tmp_path / "manifest.jsonl"
    manifest.touch()
    holder = os.open(manifest, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(driver, "HARNESS_LOCK_TIMEOUT_SECONDS", 0.03)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring manifest lock"):
            driver._append_manifest_record(manifest, {"instance_id": "task-1"})
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_smoke_manifest_reports_directory_fsync_failure(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")

    def fail_directory_fsync(_path):
        raise OSError("manifest directory fsync failed")

    monkeypatch.setattr(driver, "_fsync_directory", fail_directory_fsync)
    manifest = tmp_path / "manifest.jsonl"
    with pytest.raises(OSError, match="manifest directory fsync failed"):
        driver._append_manifest_record(manifest, {"instance_id": "task-1"})

    assert json.loads(manifest.read_text(encoding="utf-8"))["instance_id"] == "task-1"


def test_smoke_manifest_appends_multiple_records_and_repairs_truncated_tail(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"truncated":true}', encoding="utf-8")

    driver._append_manifest_record(manifest, {"instance_id": "task-1"})
    driver._append_manifest_record(manifest, {"instance_id": "task-2"})

    assert manifest.read_text(encoding="utf-8").splitlines() == [
        '{"truncated":true}',
        json.dumps({"instance_id": "task-1"}),
        json.dumps({"instance_id": "task-2"}),
    ]


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX fork")
def test_smoke_generator_permanently_blocked_popen_is_reaped(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    helper_pid = tmp_path / "helper.pid"

    def block_forever(*args, **kwargs):
        del args, kwargs
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        helper_pid.write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(1)

    monkeypatch.setattr(driver, "_GENERATOR_POPEN", block_forever)
    started = time.monotonic()

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=0.2,
        spawn_timeout=0.05,
        term_timeout=0.05,
        kill_timeout=0.2,
    )

    assert time.monotonic() - started < 1.0
    assert returncode == driver.TECHNICAL_EXIT_CODE
    assert "spawn exceeded" in reason
    pid = int(helper_pid.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX sessions")
def test_smoke_generator_normal_exit_cleans_lingering_descendant(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    started = tmp_path / "descendant.started"
    finished = tmp_path / "descendant.finished"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(started)!r}).touch();"
        "time.sleep(0.8);"
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "time.sleep(0.1)"
    )

    returncode, reason = driver._run_generator(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        outer_timeout=1.0,
        spawn_timeout=0.2,
        term_timeout=0.05,
        kill_timeout=0.3,
    )

    assert returncode == 0
    assert reason == ""
    assert started.exists()
    time.sleep(0.9)
    assert not finished.exists()


def test_smoke_manifest_rejects_symlinked_parent_without_outside_write(tmp_path):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        driver._append_manifest_record(
            linked / "manifest.jsonl",
            {"instance_id": "task-1"},
        )

    assert list(outside.iterdir()) == []


def test_smoke_main_rejects_symlinked_output_parent_before_artifact_write(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.run_swebench_smoke_batch")
    instances = tmp_path / "instances"
    instances.mkdir()
    (instances / "task.json").write_text(
        json.dumps({"instance_id": "task-1"}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_swebench_smoke_batch.py",
            "--instances-dir",
            str(instances),
            "--output-dir",
            str(linked / "output"),
        ],
    )

    with pytest.raises(SystemExit) as captured:
        driver.main()

    assert captured.value.code == 2
    assert list(outside.iterdir()) == []
