from __future__ import annotations

from swe_eval_status_support import (
    _patch,
    _write_jsonl,
    _write_ready_eval_pair,
    build_snapshots,
    discover_eval_reports,
    discovery_mod,
    importlib,
    json,
    os,
    pytest,
    row_patch_sha,
    signal,
    subprocess,
    sys,
    task_status_row,
    time,
)


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
