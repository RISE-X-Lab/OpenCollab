from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    Path,
    _remote_config,
    _remote_namespace,
    _write_jsonl,
    contextmanager,
    fcntl,
    json,
    os,
    pytest,
    runner,
    signal,
    subprocess,
    sys,
    threading,
)


def test_remote_read_jsonl_fails_when_rows_exceed_bounded_capacity(
    tmp_path,
    monkeypatch,
):
    namespace = _remote_namespace(tmp_path)
    namespace["MAX_JSONL_LINE_BYTES"] = 128
    namespace["MAX_JSONL_RETAINED_BYTES"] = 1024
    namespace["MAX_JSONL_RETAINED_ROWS"] = 2
    path = tmp_path / "large.jsonl"
    path.write_bytes(b"".join((json.dumps({"index": index}) + "\n").encode("utf-8") for index in range(3)))

    def forbidden_read_text(*args, **kwargs):
        raise AssertionError("read_jsonl must not load the whole file")

    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    with pytest.raises(namespace["RecordInputLimitError"], match="row or byte"):
        namespace["read_jsonl"](path)


def test_remote_read_jsonl_fails_when_file_exceeds_scan_capacity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["MAX_JSONL_SCAN_BYTES"] = 32
    path = tmp_path / "large.jsonl"
    path.write_text(json.dumps({"payload": "x" * 64}) + "\n", encoding="utf-8")

    with pytest.raises(namespace["RecordInputLimitError"], match="exceeds 32 bytes"):
        namespace["read_jsonl"](path)


@pytest.mark.parametrize("bad_line", [b'{"broken":}\n', b"\xff\n", b"[]\n", b"\n"])
def test_remote_read_jsonl_rejects_invalid_physical_record(tmp_path, bad_line):
    namespace = _remote_namespace(tmp_path)
    path = tmp_path / "records.jsonl"
    path.write_bytes(bad_line + b'{"instance_id":"later"}\n')

    with pytest.raises(namespace["RecordInputFormatError"]):
        namespace["read_jsonl"](path)


def test_remote_generation_scan_refuses_to_forget_old_task(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["MAX_JSONL_RETAINED_ROWS"] = 2
    run_dir = namespace["base_run_dir"] / "task-old"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": "task-old",
                "record_id": "old-record",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            },
            {"instance_id": "task-new-1"},
            {"instance_id": "task-new-2"},
        ],
    )

    with pytest.raises(namespace["RecordInputLimitError"]):
        namespace["generation_done"](run_dir, "task-old")


def test_remote_read_jsonl_rejects_symlink(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = tmp_path / "target.jsonl"
    _write_jsonl(target, [{"instance_id": "task-1"}])
    link = tmp_path / "records.jsonl"
    link.symlink_to(target)

    with pytest.raises(OSError, match="regular file"):
        namespace["read_jsonl"](link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_remote_read_jsonl_rejects_fifo_without_blocking(tmp_path):
    namespace = _remote_namespace(tmp_path)
    path = tmp_path / "records.jsonl"
    os.mkfifo(path)

    with pytest.raises(OSError, match="regular file"):
        namespace["read_jsonl"](path)


def test_remote_log_tail_uses_seek_and_bounded_read(tmp_path, monkeypatch):
    namespace = _remote_namespace(tmp_path)
    path = tmp_path / "large.log"
    path.write_bytes(b"a" * 1_000_000 + b"TAIL")
    original_open_regular = namespace["open_regular_binary"]
    read_sizes = []

    class TrackingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def seek(self, *args):
            return self._wrapped.seek(*args)

        def fileno(self):
            return self._wrapped.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 <= size <= 32
            return self._wrapped.read(size)

    @contextmanager
    def tracked_open(path):
        with original_open_regular(path) as handle:
            yield TrackingReader(handle)

    monkeypatch.setitem(namespace, "open_regular_binary", tracked_open)
    tail = namespace["read_tail_text"](path, 32)

    assert tail.endswith("TAIL")
    assert len(tail.encode()) == 32
    assert read_sizes == [32]


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_remote_log_tail_rejects_unsafe_container_artifact_without_blocking(
    tmp_path,
    kind,
):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    namespace = _remote_namespace(tmp_path)
    path = tmp_path / "container.log"
    if kind == "symlink":
        target = tmp_path / "target.log"
        target.write_text("secret", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises(OSError):
        namespace["read_tail_text"](path, 32)


def test_remote_atomic_json_rejects_final_symlink_without_touching_target(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "summary.json"
    link.symlink_to(target)

    with pytest.raises(OSError, match="regular or absent"):
        namespace["write_json"](link, {"status": "done"})

    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_remote_jsonl_append_rejects_unsafe_target_without_blocking(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    namespace = _remote_namespace(tmp_path)
    path = tmp_path / "events.jsonl"
    if kind == "symlink":
        target = tmp_path / "target.jsonl"
        target.write_text("unchanged\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises(OSError, match="regular"):
        namespace["append_jsonl"](path, {"event": "x"})


def test_remote_jsonl_append_lock_wait_is_bounded(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["HARNESS_LOCK_TIMEOUT_SECONDS"] = 0.03
    path = tmp_path / "events.jsonl"
    path.touch()
    holder = os.open(path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring"):
            namespace["append_jsonl"](path, {"event": "x"})
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_remote_runner_pid_rejects_preexisting_symlink(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "pid-target"
    target.write_text("unchanged", encoding="utf-8")
    (run_dir / "runner.pid").symlink_to(target)

    namespace = _remote_namespace(tmp_path)
    namespace["RUNNER_LOCK_FD"] = None
    namespace["RUNNER_OWNER_RECORD"] = None
    namespace["process_start_identity"] = lambda pid: "proc:test"

    with pytest.raises(OSError, match="regular file"):
        namespace["write_runner_pid"]()

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_remote_run_directory_allows_only_one_live_runner_process(tmp_path):
    first_cfg = _remote_config(tmp_path, owner_nonce="a" * 32)
    second_cfg = {**first_cfg, "owner_nonce": "b" * 32}
    remote_code = (
        "import json, sys\n"
        "from opencollab.harness.swe_v1_remote_runner import install_into\n"
        "install_into(globals(), json.loads(sys.stdin.read()))\n"
        "process_start_identity = lambda pid: f'test:{pid}'\n"
        "initialize_runner_ownership()\n"
    )
    owner_code = remote_code + "print('owned', flush=True)\n" + "import time\n" + "time.sleep(30)\n"
    contender_code = remote_code + "print('unexpected-owner', flush=True)\n"
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "opencollab")
    first = subprocess.Popen(
        [sys.executable, "-c", owner_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=child_env,
    )
    try:
        assert first.stdin is not None
        first.stdin.write(json.dumps(first_cfg))
        first.stdin.close()
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "owned"

        second = subprocess.run(
            [sys.executable, "-c", contender_code],
            input=json.dumps(second_cfg),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            env=child_env,
        )

        assert second.returncode != 0
        assert "another ProLite runner owns this run directory" in second.stderr
        owner = json.loads((Path(first_cfg["base_run_dir"]) / "runner.pid").read_text(encoding="utf-8"))
        assert owner["owner_nonce"] == "a" * 32
    finally:
        try:
            os.killpg(first.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        first.wait(timeout=5)


def test_remote_runner_reclaims_stale_owner_with_identity_evidence(tmp_path):
    namespace = _remote_namespace(tmp_path, owner_nonce="b" * 32)
    namespace["RUNNER_LOCK_FD"] = None
    namespace["RUNNER_OWNER_RECORD"] = None
    run_dir = namespace["base_run_dir"]
    run_dir.mkdir(parents=True, exist_ok=True)
    stale = {
        "schema": "opencollab.prolite_runner_owner.v1",
        "pid": 424242,
        "start_identity": "proc:old",
        "owner_nonce": "a" * 32,
    }
    (run_dir / "runner.pid").write_text(json.dumps(stale), encoding="utf-8")
    namespace["process_start_identity"] = lambda pid: "proc:self" if pid == os.getpid() else ""
    namespace["_pid_exists"] = lambda pid: False

    owner = namespace["write_runner_pid"]()

    assert owner["owner_nonce"] == "b" * 32
    assert json.loads((run_dir / "runner.pid").read_text(encoding="utf-8")) == owner
    os.close(namespace["RUNNER_LOCK_FD"])


def test_remote_start_state_requires_owner_lock_and_serializes_rmw(tmp_path):
    namespace = _remote_namespace(tmp_path)
    run_dir = namespace["base_run_dir"] / "task-1"
    namespace["RUNNER_LOCK_FD"] = None
    with pytest.raises(RuntimeError, match="ownership lock"):
        namespace["write_start_state"](run_dir, "task-1", "session")

    namespace["RUNNER_LOCK_FD"] = -1
    threads = [
        threading.Thread(
            target=namespace["write_start_state"],
            args=(run_dir, "task-1", f"session-{index}"),
        )
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    state = json.loads(namespace["generation_state_path"](run_dir).read_text(encoding="utf-8"))
    assert state["start_count"] == 12
    assert len(state["starts"]) == 12


def test_remote_dataset_loader_streams_only_requested_slice(tmp_path, monkeypatch):
    namespace = _remote_namespace(tmp_path)
    dataset = namespace["dataset_path"]
    _write_jsonl(
        dataset,
        [{"instance_id": f"task-{index}"} for index in range(100)],
    )
    original_iter = namespace["iter_jsonl"]
    yielded = 0

    def tracked_iter(*args, **kwargs):
        nonlocal yielded
        for item in original_iter(*args, **kwargs):
            yielded += 1
            yield item

    monkeypatch.setitem(namespace, "iter_jsonl", tracked_iter)
    rows = namespace["load_dataset"](3, 2)

    assert [row["instance_id"] for row in rows] == ["task-2", "task-3"]
    assert yielded == 4


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_remote_dataset_loader_rejects_fifo_without_blocking(tmp_path):
    namespace = _remote_namespace(tmp_path)
    dataset = namespace["dataset_path"]
    dataset.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(dataset)

    with pytest.raises(OSError, match="regular file"):
        namespace["load_dataset"](1, 1)


def test_remote_dataset_loader_rejects_symlink(tmp_path):
    namespace = _remote_namespace(tmp_path)
    dataset = namespace["dataset_path"]
    dataset.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "dataset-target.jsonl"
    _write_jsonl(target, [{"instance_id": "task-1"}])
    dataset.symlink_to(target)

    with pytest.raises(OSError, match="regular file"):
        namespace["load_dataset"](1, 1)


def test_remote_dataset_loader_rejects_bad_physical_row_without_slice_drift(tmp_path):
    namespace = _remote_namespace(tmp_path)
    dataset = namespace["dataset_path"]
    dataset.parent.mkdir(parents=True, exist_ok=True)
    dataset.write_bytes(b'{"instance_id":"task-1"}\n{"broken":}\n{"instance_id":"task-3"}\n')

    with pytest.raises(namespace["RecordInputFormatError"]):
        namespace["load_dataset"](2, 1)


def test_remote_dataset_loader_bounds_total_bytes(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["MAX_DATASET_BYTES"] = 32
    dataset = namespace["dataset_path"]
    _write_jsonl(dataset, [{"instance_id": "task-" + "x" * 64}])

    with pytest.raises(namespace["RecordInputLimitError"], match="exceeds 32 bytes"):
        namespace["load_dataset"](1, 1)


def test_remote_dataset_loader_bounds_physical_rows(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["MAX_DATASET_ROWS"] = 2
    dataset = namespace["dataset_path"]
    _write_jsonl(
        dataset,
        [{"instance_id": f"task-{index}"} for index in range(3)],
    )

    with pytest.raises(namespace["RecordInputLimitError"], match="physical rows"):
        namespace["load_dataset"](1, 3)


@pytest.mark.parametrize(
    "instance_id",
    [
        "",
        ".",
        "..",
        "../../escape",
        "/absolute/task",
        r"C:\\escape",
        "nested/task",
        r"nested\\task",
        "task\nname",
        "task\u200dname",
        "x" * 241,
        "\ud800",
    ],
)
def test_remote_dataset_loader_rejects_unsafe_task_path_component(
    tmp_path,
    instance_id,
):
    namespace = _remote_namespace(tmp_path)
    _write_jsonl(namespace["dataset_path"], [{"instance_id": instance_id}])

    with pytest.raises(ValueError, match="instance_id"):
        namespace["load_dataset"](1, 1)


def test_remote_dataset_loader_accepts_normal_task_component(tmp_path):
    namespace = _remote_namespace(tmp_path)
    row = {"instance_id": "django__django-12345"}
    _write_jsonl(namespace["dataset_path"], [row])

    assert namespace["load_dataset"](1, 1) == [row]


def test_remote_fifo_writer_handles_partial_and_retryable_writes(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    writes = []
    outcomes = [2, BlockingIOError(), 3]
    monkeypatch.setattr(namespace["os"], "open", lambda *args, **kwargs: 91)
    monkeypatch.setattr(namespace["os"], "close", lambda fd: None)

    def fake_write(fd, payload):
        writes.append(bytes(payload))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(namespace["os"], "write", fake_write)

    result = namespace["write_fifo_with_timeout"](
        tmp_path / "input.fifo",
        "hello",
        timeout=1,
    )

    assert result == {"ok": True}
    assert writes == [b"hello", b"llo", b"llo"]


def test_local_report_pair_publishes_matching_bundle_identity(tmp_path):
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"

    runner.write_local_report(
        {"status": "done", "markdown": "# Report\n"},
        json_path,
        md_path,
    )

    bundle_id = json.loads(json_path.read_text(encoding="utf-8"))["local_report_bundle_id"]
    assert f"local_report_bundle_id:{bundle_id}" in md_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("target_name", ["report.json", "report.md"])
def test_local_report_rejects_symlink_destination_without_touching_target(
    tmp_path,
    target_name,
):
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    (tmp_path / target_name).symlink_to(victim)

    with pytest.raises(OSError, match="regular or absent"):
        runner.write_local_report(
            {"status": "done", "markdown": "# Report\n"},
            json_path,
            md_path,
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_remote_output_capture_keeps_only_bounded_tail(monkeypatch):
    monkeypatch.setattr(runner, "MAX_REMOTE_OUTPUT_TAIL_CHARS", 128)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('x' * 10000); print('y' * 10000, file=sys.stderr)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    stdout, stderr = runner._bounded_remote_communicate(
        proc,
        "payload",
        timeout=5,
    )

    assert stdout.startswith("[truncated ")
    assert stderr.startswith("[truncated ")
    assert len(stdout) < 256
    assert len(stderr) < 256
