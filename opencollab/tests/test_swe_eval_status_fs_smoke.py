from __future__ import annotations

from swe_eval_status_support import (
    _auto_eval_summary,
    fcntl,
    importlib,
    json,
    os,
    pytest,
    signal,
    stat,
    sys,
    time,
)


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

    assert not output.exists()
    retired = list(tmp_path.glob(".opencollab-retired-*"))
    assert len(retired) == 2
    assert len({(entry.stat().st_dev, entry.stat().st_ino) for entry in retired}) == 1
    assert list(tmp_path.glob(".oc-*.tmp")) == []


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
    assert list(tmp_path.glob(".oc-*.tmp")) == []


def test_auto_eval_atomic_write_rejects_parent_replacement(tmp_path, monkeypatch):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    safe_state = importlib.import_module("scripts.swe_auto_eval_safe_state")
    parent = tmp_path / "state"
    parent.mkdir()
    moved_parent = tmp_path / "state-moved"
    output = parent / "status.json"
    output.write_text('{"status":"old"}\n', encoding="utf-8")
    original_write = safe_state.write_regular_bytes_atomic

    def replace_parent_before_commit(path, payload, **kwargs):
        parent.rename(moved_parent)
        parent.mkdir()
        output.write_text("foreign\n", encoding="utf-8")
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(
        safe_state,
        "write_regular_bytes_atomic",
        replace_parent_before_commit,
    )

    with pytest.raises(OSError, match="parent identity changed"):
        driver._write_json(output, {"status": "new"})

    assert output.read_text(encoding="utf-8") == "foreign\n"
    assert (moved_parent / "status.json").read_text(encoding="utf-8") == '{"status":"old"}\n'


def test_per_instance_atomic_write_preserves_primary_error_when_retirement_sync_fails(
    tmp_path,
    monkeypatch,
):
    runner = importlib.import_module("scripts.run_swebench_eval_per_instance")
    calls = 0

    def fail_fsync(_fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("primary file fsync failure")
        raise OSError("secondary retirement fsync failure")

    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    output = tmp_path / "record.json"

    with pytest.raises(OSError, match="primary file fsync failure") as captured:
        runner._write_json_atomic(output, {"status": "new"})

    assert any("secondary retirement fsync failure" in note for note in getattr(captured.value, "__notes__", []))
    assert len(list(tmp_path.glob(".opencollab-retired-*"))) == 1


def test_auto_eval_atomic_write_preserves_primary_error_when_retirement_sync_fails(
    tmp_path,
    monkeypatch,
):
    driver = importlib.import_module("scripts.swe_auto_eval_driver")
    calls = 0

    def fail_fsync(_fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("primary file fsync failure")
        raise OSError("secondary retirement fsync failure")

    monkeypatch.setattr(driver.os, "fsync", fail_fsync)
    output = tmp_path / "record.json"

    with pytest.raises(OSError, match="primary file fsync failure") as captured:
        driver._write_json(output, {"status": "new"})

    assert any("secondary retirement fsync failure" in note for note in getattr(captured.value, "__notes__", []))
    assert len(list(tmp_path.glob(".opencollab-retired-*"))) == 1


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
    parent_code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_code!r}]);time.sleep(0.1)"

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
            "--model-name",
            "test-model",
        ],
    )

    with pytest.raises(SystemExit) as captured:
        driver.main()

    assert captured.value.code == 2
    assert list(outside.iterdir()) == []
