from __future__ import annotations

from swe_eval_status_support import (
    Path,
    SimpleNamespace,
    _patch,
    _write_jsonl,
    _write_ready_eval_pair,
    build_snapshots,
    discovery_mod,
    importlib,
    json,
    os,
    pytest,
    records_mod,
    row_patch_sha,
    signal,
    subprocess,
    sys,
    task_status_row,
    threading,
    time,
)


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
        target=lambda: results.append(driver._acquire_claim(claim_path, {"pid": os.getpid(), "owner": "first"}))
    )
    second = threading.Thread(
        target=lambda: results.append(driver._acquire_claim(claim_path, {"pid": os.getpid(), "owner": "second"}))
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
    command = f"{sys.executable} -c 'import time; print(\"CHILD_OUTPUT\"); time.sleep(5)'"

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
        f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(0.05)"
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
    command_code = f"import pathlib,time; time.sleep(1.5); pathlib.Path({str(sentinel)!r}).write_text('leaked')"
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
                    "evaluator_start_identity": driver._process_start_identity(process.pid),
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
                "patch_sha256": row_patch_sha(records_mod.read_jsonl(run_dir / "predictions.jsonl")[0]),
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
