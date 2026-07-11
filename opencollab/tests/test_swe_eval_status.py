from __future__ import annotations

from swe_eval_status_support import (
    SUBMISSION_INTEGRITY_INELIGIBLE,
    SUBMISSION_INTEGRITY_LEGACY,
    SUBMISSION_INTEGRITY_PROVEN,
    EvalReport,
    Path,
    TaskState,
    _patch,
    _write_jsonl,
    build_snapshots,
    build_snapshots_from_rows,
    contextmanager,
    decide_task,
    importlib,
    is_completed_prediction,
    json,
    latest_paired_rows,
    metric_submission_integrity,
    os,
    patch_sha,
    patch_sha_matches,
    pytest,
    records_mod,
    row_patch_sha,
    task_status_row,
)


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
