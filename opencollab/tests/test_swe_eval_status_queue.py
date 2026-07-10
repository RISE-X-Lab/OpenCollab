from __future__ import annotations

from swe_eval_status_support import (
    TaskState,
    _patch,
    _strict_modern_prediction,
    _write_jsonl,
    build_snapshots,
    decide_task,
    discover_eval_reports,
    importlib,
    json,
    os,
    pytest,
    records_mod,
    row_patch_sha,
    sys,
    time,
)


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
        json.dumps({"instance_id": "task-1", "model_patch": "+x"}) + "\n" + '{"instance_id":',
        encoding="utf-8",
    )

    with pytest.raises(records_mod.RecordInputFormatError, match="invalid JSONL"):
        runner.read_jsonl(path)


def test_per_instance_queue_accepts_jsonl_dataset(tmp_path):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    dataset_path = tmp_path / "instances.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    dataset_path.write_text(
        json.dumps({"instance_id": "task-1"}) + "\n" + json.dumps({"instance_id": "task-2"}) + "\n",
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

    assert path == (tmp_path / "logs" / "run_evaluation" / "run" / "org__model" / "task-1" / "report.json")


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
