from __future__ import annotations

from swe_eval_status_support import (
    SimpleNamespace,
    fcntl,
    importlib,
    json,
    os,
    pytest,
    records_mod,
    subprocess,
    sys,
    threading,
    time,
)


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
                handle.write(json.dumps({"instance_id": "task-2", "model_patch": "+two"}) + "\n")
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
    linked_report = work_dir / "logs" / "run" / "model" / "task-1" / "report.json"

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
