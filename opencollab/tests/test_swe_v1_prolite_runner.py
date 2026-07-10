from __future__ import annotations

import fcntl
import importlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

runner = importlib.import_module("scripts.swe_v1_prolite_runner")


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--budget", "-1"),
        ("--max-steps", "0"),
        ("--swe-timeout", "0"),
        ("--task-wall-timeout", "-2"),
        ("--eval-timeout", "0"),
        ("--llm-timeout", "0"),
        ("--total-timeout", "-3"),
        ("--checkpoint-interval", "-1"),
        ("--limit", "1001"),
        ("--run-id", "../../escape"),
    ],
)
def test_main_rejects_invalid_numeric_limits_before_dry_run(
    monkeypatch, option, value
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swe_v1_prolite_runner.py", "--dry-run", option, value],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2


def _remote_config(tmp_path, **overrides):
    remote_root = tmp_path / "remote"
    remote_repo = remote_root / "repo"
    remote_repo.mkdir(parents=True, exist_ok=True)
    package_link = remote_repo / "opencollab"
    if not package_link.exists():
        package_link.symlink_to(
            _REPO_ROOT / "opencollab",
            target_is_directory=True,
        )
    base_run_dir = tmp_path / "run"
    cfg = {
        "token": "tok",
        "owner_nonce": "a" * 32,
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
        "session_prefix": "test",
        "remote_proxy_base_url": "http://127.0.0.1:18788",
        "start_index": 1,
        "limit": 1,
        "budget": 1000,
        "max_steps": 3,
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "llm_timeout": 10,
        "checkpoint_interval": 300,
        "max_task_starts": 1,
        "dry_run": False,
    }
    cfg.update(overrides)
    return cfg


def _remote_namespace(tmp_path, **overrides):
    cfg = _remote_config(tmp_path, **overrides)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(cfg))
    namespace = {"__name__": "swe_v1_remote_runner_test"}
    remote_code = runner.REMOTE_RUNNER.rsplit("raise SystemExit(main())", 1)[0]
    try:
        exec(remote_code, namespace)
    finally:
        sys.stdin = old_stdin
    namespace["RUNNER_LOCK_FD"] = -1
    namespace["RUNNER_OWNER_RECORD"] = {
        "owner_nonce": cfg["owner_nonce"],
        "pid": os.getpid(),
    }
    return namespace


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _test_only_patch() -> str:
    return (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n"
        "+++ b/tests/test_widget.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test_widget(): pass\n"
    )


def _seed_remote_completed_generation(namespace, task: str = "task-1") -> None:
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
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
    path.write_bytes(
        b"".join(
            (json.dumps({"index": index}) + "\n").encode("utf-8")
            for index in range(3)
        )
    )

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
    remote_code = runner.REMOTE_RUNNER.rsplit("raise SystemExit(main())", 1)[0]
    remote_code = remote_code.replace(
        'if __name__ == "__main__":\n    initialize_runner_ownership()',
        'if False:\n    initialize_runner_ownership()',
    )
    owner_code = (
        remote_code
        + "\nprocess_start_identity = lambda pid: f'test:{pid}'\n"
        + "initialize_runner_ownership()\n"
        + "print('owned', flush=True)\n"
        + "time.sleep(30)\n"
    )
    contender_code = (
        remote_code
        + "\nprocess_start_identity = lambda pid: f'test:{pid}'\n"
        + "initialize_runner_ownership()\n"
        + "print('unexpected-owner', flush=True)\n"
    )
    first = subprocess.Popen(
        [sys.executable, "-c", owner_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
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
        )

        assert second.returncode != 0
        assert "another ProLite runner owns this run directory" in second.stderr
        owner = json.loads(
            (Path(first_cfg["base_run_dir"]) / "runner.pid").read_text(
                encoding="utf-8"
            )
        )
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
    namespace["process_start_identity"] = lambda pid: (
        "proc:self" if pid == os.getpid() else ""
    )
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

    state = json.loads(
        namespace["generation_state_path"](run_dir).read_text(encoding="utf-8")
    )
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
    dataset.write_bytes(
        b'{"instance_id":"task-1"}\n'
        b'{"broken":}\n'
        b'{"instance_id":"task-3"}\n'
    )

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
        text=True,
        start_new_session=True,
    )
    deadline = runner.time.monotonic() + 2
    while not ready.exists() and runner.time.monotonic() < deadline:
        runner.time.sleep(0.01)
    if not ready.exists():
        runner.os.killpg(process.pid, runner.signal.SIGKILL)
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
        text=True,
        start_new_session=True,
    )


def test_remote_runner_rejects_invalid_slice_config(tmp_path):
    namespace = _remote_namespace(tmp_path, start_index=0, limit=0, max_task_starts=-1)

    errors = namespace["validate_runner_config"]()

    assert "start_index must be >= 1" in errors
    assert "limit must be > 0" in errors
    assert "max_task_starts must be >= 0" in errors


def test_remote_runner_rejects_excessive_slice_limit(tmp_path):
    namespace = _remote_namespace(tmp_path, limit=1001)

    assert "limit must be <= 1000" in namespace["validate_runner_config"]()


def test_remote_runner_allows_eval_only_mode_with_existing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"


def test_remote_runner_eval_only_mode_does_not_start_missing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0)

    result = namespace["generation_for_task"]({"instance_id": "task-1"})

    assert result["status"] == "generation_start_limit_reached"


def test_remote_runner_skips_eval_after_generation_cleanup_failure(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["remote_root"].mkdir(parents=True, exist_ok=True)
    namespace["remote_repo"].mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].write_text("[]\n", encoding="utf-8")
    namespace["http_health"] = lambda *args, **kwargs: {"ok": True}
    namespace["load_dataset"] = lambda *_args: [{"instance_id": "task-1"}]
    namespace["generation_for_task"] = lambda row: {
        "status": "technical_generation_cleanup_failed",
        "task": row["instance_id"],
    }
    eval_calls = []
    namespace["eval_for_task"] = lambda row: eval_calls.append(row) or {
        "status": "eval_done"
    }

    returncode = namespace["main"]()
    summary = json.loads(
        (namespace["base_run_dir"] / "summary.json").read_text(encoding="utf-8")
    )

    assert returncode == 1
    assert eval_calls == []
    assert summary["rows"][0]["eval"] == {
        "status": "skipped_generation_not_ready",
        "task": "task-1",
        "generation_status": "technical_generation_cleanup_failed",
        "reason": "generation_not_ready",
    }


def test_remote_runner_recovers_committed_prediction_without_metrics_projection(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    namespace["write_json"](
        namespace["generation_state_path"](run_dir),
        {"start_count": 1},
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["pairing"] == "embedded_metric"


@pytest.mark.parametrize(
    ("status", "returncode", "expected"),
    [
        ("done", 0, True),
        ("done", 1, False),
        ("done_with_timeout_patch", 124, True),
        ("done_with_timeout_patch", 1, False),
        ("done_with_timeout_patch", 0, False),
        ("done", True, False),
        ("done", None, False),
    ],
)
def test_remote_generation_done_requires_strict_status_returncode_identity(
    tmp_path,
    status,
    returncode,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": status,
    }
    if returncode is not None:
        metric["runner_returncode"] = returncode
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](
        run_dir, task
    )

    assert done is expected
    assert pairing == "embedded_metric"


@pytest.mark.parametrize(
    ("integrity_fields", "expected"),
    [
        ({}, True),
        ({"submission_eligible": False}, False),
        ({"execution_quiesced": False}, False),
        ({"test_patch_isolation_failed": True}, False),
    ],
)
def test_remote_generation_done_rejects_explicit_integrity_failure_but_keeps_legacy(
    tmp_path,
    integrity_fields,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        **integrity_fields,
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](
        run_dir,
        task,
    )

    assert done is expected
    assert pairing == "embedded_metric"


def test_remote_runner_rejects_test_only_patch_before_eval(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, _pairing = namespace["generation_done"](run_dir, task)
    result = namespace["eval_for_task"]({"instance_id": task})

    assert done is False
    assert result["status"] == "empty_eval_patch_invalid"
    assert result["summary"]["eval_model_patch_chars"] == 0
    assert result["summary"]["technical_reasons"] == ["empty_eval_patch_after_filter"]


def test_filter_model_patch_handles_diff_paths_with_spaces(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/src/app code.py b/src/app code.py\n"
        "--- a/src/app code.py\n"
        "+++ b/src/app code.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/tests/test app.py b/tests/test app.py\n"
        "--- a/tests/test app.py\n"
        "+++ b/tests/test app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/app code.py" in filtered
    assert "tests/test app.py" not in filtered


def test_filter_model_patch_decodes_quoted_octal_git_paths(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        'diff --git "a/src/\\346\\250\\241\\345\\235\\227.py" '
        '"b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '--- "a/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '+++ "b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/test_\\344\\270\\255.py" "b/test_\\344\\270\\255.py"\n'
        '--- "a/test_\\344\\270\\255.py"\n'
        '+++ "b/test_\\344\\270\\255.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/src\\\\tests\\\\module.py" "b/src\\\\tests\\\\module.py"\n'
        '--- "a/src\\\\tests\\\\module.py"\n'
        '+++ "b/src\\\\tests\\\\module.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/\\346\\250\\241\\345\\235\\227.py" in filtered
    assert "test_\\344\\270\\255.py" not in filtered
    assert "src\\\\tests\\\\module.py" in filtered


def test_prolite_prediction_sha_comes_from_patch_text(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    stale_patch = "diff --git a/src/a.py b/src/a.py\n+stale\n"
    current_patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    stale_sha = namespace["patch_sha"](stale_patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "model_patch": current_patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert namespace["row_patch_sha"](prediction) == namespace["patch_sha"](current_patch)
    assert done is False
    assert pairing == "record_id_patch_sha_mismatch"


def test_remote_patch_sha_match_requires_exact_hex_digest(tmp_path):
    namespace = _remote_namespace(tmp_path)
    digest = "a1" * 32

    assert namespace["patch_sha_matches"](digest, digest) is True
    assert namespace["patch_sha_matches"](digest[:12], digest) is False
    assert namespace["patch_sha_matches"]("g" * 64, "g" * 64) is False
    assert namespace["patch_sha_matches"](digest.upper(), digest) is False


def test_remote_spawn_guard_does_not_block_sigterm_in_exec_child(tmp_path):
    namespace = _remote_namespace(tmp_path)
    spawn_signal_state = namespace["block_spawn_signals"]()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    namespace["ACTIVE_CHILD_PGIDS"].add(proc.pid)
    namespace["restore_spawn_signals"](spawn_signal_state)
    try:
        namespace["os"].killpg(proc.pid, namespace["signal"].SIGTERM)
        returncode = proc.wait(timeout=1)
    finally:
        namespace["ACTIVE_CHILD_PGIDS"].discard(proc.pid)
        if proc.poll() is None:
            namespace["os"].killpg(proc.pid, namespace["signal"].SIGKILL)
            proc.wait(timeout=1)

    assert returncode == -namespace["signal"].SIGTERM


def test_remote_active_child_cleanup_escalates_to_kill_and_proves_absence(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    namespace["ACTIVE_CHILD_PGIDS"].add(424299)
    monkeypatch.setitem(namespace, "PROCESS_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setitem(namespace, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.0)
    signals = []
    probes = 0

    def group_exists(_pgid):
        nonlocal probes
        probes += 1
        return probes == 1

    monkeypatch.setitem(namespace, "process_group_exists", group_exists)
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    assert namespace["terminate_active_children"]() is True
    assert signals == [
        (424299, namespace["signal"].SIGTERM),
        (424299, namespace["signal"].SIGKILL),
    ]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()


def test_generation_timeout_recovers_completed_candidate(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                _write_jsonl(
                    run_dir / "predictions.jsonl",
                    [
                        {
                            "instance_id": task,
                            "record_id": "r1",
                            "patch_sha256": patch_sha,
                            "model_patch": patch,
                        }
                    ],
                )
                _write_jsonl(
                    run_dir / "metrics.jsonl",
                    [
                        {
                            "instance_id": task,
                            "record_id": "r1",
                            "patch_sha256": patch_sha,
                            "workflow_status": "done",
                            "runner_returncode": 0,
                        }
                    ],
                )
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["returncode"] == 124
    assert result["timed_out"] is True


def test_generation_timeout_reports_stubborn_kill_reap(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    release = threading.Event()
    consumer_started = threading.Event()
    task = "task-1"
    signals = []

    class StubbornProcess:
        pid = 424243

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    try:
        result = namespace["generation_for_task"]({"instance_id": task})
    finally:
        release.set()

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert signals == [
        (424243, namespace["signal"].SIGTERM),
        (424243, namespace["signal"].SIGKILL),
    ]
    assert consumer_started.wait(timeout=0.2)


def test_generation_normal_exit_cleanup_failure_is_technical(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"

    class ReapedLeader:
        pid = 424262

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: False

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert 424262 in namespace["ACTIVE_CHILD_PGIDS"]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_generation_wait_system_exit_terminates_child_and_re_raises(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class InterruptedProcess:
        pid = 424250

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise SystemExit(77)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 77
    assert signals == [(424250, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_generation_spawn_registration_precedes_pending_signal_restore(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class SpawnedProcess:
        pid = 424252

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: SpawnedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    real_restore = namespace["restore_spawn_signals"]

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert 424252 in namespace["ACTIVE_CHILD_PGIDS"]
        raise SystemExit(78)

    namespace["restore_spawn_signals"] = restore_then_interrupt

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 78
    assert signals == [(424252, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_eval_container_unsafe_exit_artifact_is_technical_without_blocking(
    monkeypatch,
    tmp_path,
    kind,
):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)

    class FinishedProcess:
        pid = 424270

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, *args, **kwargs):
        mount = next(item for item in command if str(item).endswith(":/eval_output"))
        output_dir = Path(str(mount).removesuffix(":/eval_output"))
        for name in (
            "service_bootstrap.exit",
            "before_repo.exit",
            "model_patch.exit",
            "test_patch.exit",
            "f2p.exit",
            "p2p.exit",
        ):
            (output_dir / name).write_text("0\n", encoding="ascii")
        unsafe = output_dir / "f2p.exit"
        unsafe.unlink()
        if kind == "symlink":
            target = output_dir / "attacker.exit"
            target.write_text("0\n", encoding="ascii")
            unsafe.symlink_to(target)
        else:
            os.mkfifo(unsafe)
        (output_dir / "f2p.log").write_text("", encoding="utf-8")
        (output_dir / "p2p.log").write_text("", encoding="utf-8")
        return FinishedProcess()

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: True

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert "unsafe_or_missing_output_artifact" in result["summary"][
        "technical_reasons"
    ]
    assert any(
        error.startswith("unsafe:f2p.exit")
        for error in result["summary"]["output_artifact_errors"]
    )


def test_eval_timeout_returns_technical_result(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert "docker_exit" in result["summary"]["technical_reasons"]


def test_eval_timeout_reports_stubborn_kill_reap(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    release = threading.Event()
    consumer_started = threading.Event()
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )

    class StubbornProcess:
        pid = 424244

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    try:
        result = namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": ["tests/test_a.py::test_a"],
                "repo_language": "python",
            }
        )
    finally:
        release.set()

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["cleanup_quiesced"] is False
    assert result["summary"]["docker_exit"] == namespace[
        "PROCESS_CLEANUP_FAILED_EXIT_CODE"
    ]
    assert "process_cleanup" in result["summary"]["technical_reasons"]
    assert consumer_started.wait(timeout=0.2)


def test_eval_normal_exit_cleanup_failure_is_technical(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )

    class ReapedLeader:
        pid = 424263

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: False
    namespace["cleanup_eval_container"] = lambda *args: {
        "ok": True,
        "status": "removed",
    }

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["cleanup_quiesced"] is False
    assert result["summary"]["docker_exit"] == namespace[
        "PROCESS_CLEANUP_FAILED_EXIT_CODE"
    ]
    assert "process_cleanup" in result["summary"]["technical_reasons"]
    assert 424263 in namespace["ACTIVE_CHILD_PGIDS"]


def test_eval_timeout_force_removes_container_from_cidfile(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_timeout=1)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )
    container_id = "a" * 64
    cleanup_calls = []

    class TimedOutProcess:
        pid = 424255

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    def fake_popen(command, *args, **kwargs):
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text(container_id, encoding="utf-8")
        return TimedOutProcess()

    def fake_run(command, timeout=60):
        cleanup_calls.append((command, timeout))
        if command[1] == "inspect":
            return {"returncode": 1, "stdout": "", "stderr": "No such container"}
        return {"returncode": 0, "stdout": container_id, "stderr": ""}

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["run"] = fake_run

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_a.py::test_a"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["container_cleanup"]["status"] == "all_references_absent"
    assert any(call[0][1] == "inspect" for call in cleanup_calls)


def test_container_cleanup_failure_preserves_recovery_markers(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_id = "b" * 64
    container_name = "opencollab-prolite-test"
    cidfile.write_text(container_id, encoding="utf-8")
    marker_path.write_text(
        json.dumps({"container_name": container_name}),
        encoding="utf-8",
    )
    namespace["run"] = lambda command, timeout=60: {
        "returncode": 125,
        "stdout": "",
        "stderr": "docker daemon unavailable",
    }

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is False
    assert result["status"] == "remove_failed"
    assert cidfile.exists()
    assert marker_path.exists()


def test_container_cleanup_uses_name_before_cidfile_exists(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-late-cid"
    marker_path.write_text(
        json.dumps({"container_name": container_name}),
        encoding="utf-8",
    )
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        if command[1] == "inspect":
            return {"returncode": 1, "stdout": "", "stderr": "No such container"}
        return {"returncode": 0, "stdout": container_name, "stderr": ""}

    namespace["run"] = fake_run

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is True
    assert result["status"] == "all_references_absent"
    assert calls == [
        (["docker", "rm", "-f", container_name], 60),
        (["docker", "inspect", "--type", "container", container_name], 30),
    ]
    assert cidfile.exists() is False
    assert marker_path.exists() is False


def test_container_cleanup_preserves_markers_until_every_reference_is_absent(tmp_path):
    namespace = _remote_namespace(tmp_path)
    cidfile = tmp_path / "container.cid"
    marker_path = tmp_path / "container.marker.json"
    container_id = "c" * 64
    container_name = "opencollab-prolite-still-present"
    cidfile.write_text(container_id, encoding="utf-8")
    marker_path.write_text("{}", encoding="utf-8")

    def fake_run(command, timeout=60):
        if command[1] == "inspect" and command[-1] == container_id:
            return {"returncode": 1, "stdout": "", "stderr": "No such container"}
        if command[1] == "inspect":
            return {"returncode": 0, "stdout": container_name, "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    namespace["run"] = fake_run

    result = namespace["cleanup_eval_container"](
        cidfile,
        marker_path,
        container_name,
    )

    assert result["ok"] is False
    assert cidfile.exists()
    assert marker_path.exists()


def test_eval_wait_interrupt_terminates_child_and_re_raises(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    run_dir = namespace["base_run_dir"] / task
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )
    signals = []

    class InterruptedProcess:
        pid = 424251

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt("eval interrupted")
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}

    with pytest.raises(KeyboardInterrupt, match="eval interrupted"):
        namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": ["tests/test_a.py::test_a"],
                "repo_language": "python",
            }
        )

    assert signals == [(424251, namespace["signal"].SIGTERM)]


def test_eval_runner_dependency_failures_are_infrastructure(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["eval_log_has_infra_failure"](
        127, "No supported JS test runner found for jest"
    ) is True
    assert namespace["eval_log_has_infra_failure"](124, "test command timed out") is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected error message 'request timed out'",
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1,
        "redis.exceptions.ConnectionError: Connection refused",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "MongoDB server unavailable: failed to connect",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected 'Connection refused' but got 'accepted'",
    ) is False


def test_prolite_go_command_uses_package_targets(tmp_path):
    namespace = _remote_namespace(tmp_path)

    command = namespace["prolite_test_command"](
        {"repo_language": "go"},
        ["internal/api/widget_test.go", "pkg/server/router_test.go"],
    )

    assert command == "go test ./internal/api ./pkg/server"


def test_ensure_image_pulls_missing_image(tmp_path):
    namespace = _remote_namespace(tmp_path)
    existing: set[str] = set()
    calls: list[list[str]] = []

    def fake_image_exists(image):
        return image in existing

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            existing.add(command[2])
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "unexpected"}

    namespace["image_exists"] = fake_image_exists
    namespace["run"] = fake_run

    result = namespace["ensure_image"]("example/image:tag")

    assert result["ok"] is True
    assert result["pulled"] is True
    assert calls == [["docker", "pull", "example/image:tag"]]


def test_image_exists_uses_bounded_docker_inspect(tmp_path):
    namespace = _remote_namespace(tmp_path)
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        return {"returncode": 124, "stdout": "", "stderr": "timed out"}

    namespace["run"] = fake_run

    assert namespace["image_exists"]("example/image:tag") is False
    assert calls == [(["docker", "image", "inspect", "example/image:tag"], 120)]


def test_remote_runner_does_not_reuse_stale_done_for_test_only_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    stale_summary = {
        "status": "done",
        "task": task,
        "patch_sha256": patch_sha,
        "record_id": "r1",
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](stale_summary, prediction, task) is False


def test_remote_runner_bootstraps_redis_for_nodebb(tmp_path):
    namespace = _remote_namespace(tmp_path)

    script = namespace["prolite_service_bootstrap"]({"repo": "NodeBB/NodeBB"})

    assert "redis-server" in script
    assert "127.0.0.1:6379" in script
    assert namespace["prolite_service_bootstrap"]({"repo": "python/cpython"}) == ""


def test_prolite_eval_commands_use_separate_input_files_not_fixed_heredocs():
    assert "<<'SERVICE'" not in runner.REMOTE_RUNNER
    assert "<<'BEFORE'" not in runner.REMOTE_RUNNER
    assert 'input_dir / "service_bootstrap.sh"' in runner.REMOTE_RUNNER
    assert 'input_dir / "before_repo.sh"' in runner.REMOTE_RUNNER
    assert "bash /eval_input/service_bootstrap.sh" in runner.REMOTE_RUNNER


def test_ensure_remote_proxy_falls_back_when_default_remote_port_is_busy():
    calls: list[list[str]] = []
    started_ports: set[int] = set()
    old_remote_http_ok = runner.remote_http_ok
    old_local_http_ok = runner.local_http_ok
    old_start_remote_proxy_tunnel = runner.start_remote_proxy_tunnel
    old_sleep = runner.time.sleep

    def fake_remote_http_ok(*, ssh_command, host, base_url, timeout=10):
        return base_url == "http://127.0.0.1:18789" and 18789 in started_ports

    def fake_start_remote_proxy_tunnel(command):
        calls.append(command)
        forward = command[command.index("-R") + 1]
        if forward.startswith("127.0.0.1:18788:"):
            return None, "Error: remote port forwarding failed for listen port 18788"
        if forward.startswith("127.0.0.1:18789:"):
            started_ports.add(18789)
            return SimpleNamespace(poll=lambda: None), ""
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.start_remote_proxy_tunnel = fake_start_remote_proxy_tunnel
        runner.time.sleep = lambda _seconds: None

        summary = runner.ensure_remote_proxy(
            ssh_command=["ssh"],
            host="jinan-aws",
            local_proxy_base_url="http://127.0.0.1:8878",
            remote_proxy_base_url="http://127.0.0.1:18788",
            enabled=True,
        )
    finally:
        runner.remote_http_ok = old_remote_http_ok
        runner.local_http_ok = old_local_http_ok
        runner.start_remote_proxy_tunnel = old_start_remote_proxy_tunnel
        runner.time.sleep = old_sleep

    assert summary["status"] == "started_fallback_port"
    assert summary["remote_proxy_base_url"] == "http://127.0.0.1:18789"
    assert summary["selected_remote_port"] == 18789
    assert "-N" in calls[1]
    assert "-fN" not in calls[1]
    assert calls[0][calls[0].index("-R") + 1] == "127.0.0.1:18788:127.0.0.1:8878"
    assert calls[1][calls[1].index("-R") + 1] == "127.0.0.1:18789:127.0.0.1:8878"


def test_proxy_tunnel_registers_before_pending_interrupt_restore(monkeypatch):
    process = SimpleNamespace(pid=424253, poll=lambda: None)
    cleanup_seen = []
    real_restore = runner._restore_local_spawn_signals

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert process in runner.REMOTE_PROXY_TUNNELS
        raise SystemExit(79)

    def fake_terminate(proc):
        cleanup_seen.append(proc)
        return True

    monkeypatch.setattr(runner, "_restore_local_spawn_signals", restore_then_interrupt)
    monkeypatch.setattr(runner, "terminate_local_process_group", fake_terminate)

    with pytest.raises(SystemExit) as exc:
        runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

    assert exc.value.code == 79
    assert cleanup_seen == [process]
    assert process not in runner.REMOTE_PROXY_TUNNELS


def test_proxy_tunnel_normal_leader_exit_cleanup_failure_is_explicit(monkeypatch):
    class ReapedTunnelLeader:
        pid = 424265
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return "", "ssh exited"

    process = ReapedTunnelLeader()
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        runner,
        "_ensure_local_process_group_quiesced_after_wait",
        lambda proc: False,
    )
    try:
        tunnel, message = runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

        assert tunnel is None
        assert "residual process-group descendants" in message
        assert process in runner.REMOTE_PROXY_TUNNELS
    finally:
        if process in runner.REMOTE_PROXY_TUNNELS:
            runner.REMOTE_PROXY_TUNNELS.remove(process)


def test_get_proxy_token_process_lookup_timeout_is_bounded(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "token_from_values", lambda values: "")

    def fake_check_output(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError, match="timed out while locating"):
        runner.get_proxy_token(tmp_path / "missing.env")

    assert calls[0][1]["timeout"] == runner.PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_proxy_env_reader_rejects_unsafe_file_without_blocking(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    path = tmp_path / "proxy.env"
    if kind == "symlink":
        target = tmp_path / "real.env"
        target.write_text("GLM_PROXY_CLIENT_TOKEN=secret\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises((OSError, RuntimeError)):
        runner.load_shell_env(path)


def test_proxy_env_reader_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_PROXY_ENV_BYTES", 32)
    path = tmp_path / "proxy.env"
    path.write_text("GLM_PROXY_CLIENT_TOKEN=" + "x" * 64, encoding="utf-8")

    with pytest.raises(RuntimeError, match="bounded regular file"):
        runner.load_shell_env(path)


def test_remote_cleanup_ps_scan_and_container_markers_are_bounded(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
    )
    remote_parts = shlex.split(calls[0][0][-1])
    cleanup_source = remote_parts[2]

    assert result["returncode"] == 0
    assert 'timeout=5' in cleanup_source
    assert 'container.cid' in cleanup_source
    assert 'container.marker.json' in cleanup_source
    assert 'scan_errors' in cleanup_source
    assert 'owner_nonce in tokens' in cleanup_source
    assert 'process_start_identity(runner_pid)' in cleanup_source
    assert 'residual_processes = scan(owner_nonce)' in cleanup_source
    assert 'cleanup_ok = not scan_errors and not residual_processes and containers_ok' in cleanup_source
    assert 'needle in args' not in cleanup_source
    assert 'raise SystemExit(0 if cleanup_ok else 3)' in cleanup_source


def test_remote_cleanup_uses_exact_nonce_token_not_run_path_substring(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run1",
    )
    cleanup_source = shlex.split(calls[0][-1])[2]

    assert "if needle in args" not in cleanup_source
    assert "owner_nonce in tokens" in cleanup_source
    assert "residual_processes" in cleanup_source


def test_remote_cleanup_scan_failures_are_reported_as_technical(monkeypatch):
    payload = {
        "ok": False,
        "status": "technical_cleanup_failed",
        "scan_errors": ["TimeoutExpired('ps', 5)", "TimeoutExpired('ps', 5)"],
        "containers": [],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=3,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = runner.terminate_remote_run(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
    )

    assert result["returncode"] == 3
    assert result["detail"]["ok"] is False
    assert result["detail"]["status"] == "technical_cleanup_failed"


def test_local_report_pair_publishes_matching_bundle_identity(tmp_path):
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"

    runner.write_local_report(
        {"status": "done", "markdown": "# Report\n"},
        json_path,
        md_path,
    )

    bundle_id = json.loads(json_path.read_text(encoding="utf-8"))[
        "local_report_bundle_id"
    ]
    assert f"local_report_bundle_id:{bundle_id}" in md_path.read_text(
        encoding="utf-8"
    )


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
            "import sys; sys.stdin.read(); print('x' * 10000); "
            "print('y' * 10000, file=sys.stderr)",
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


def test_remote_http_ok_keeps_ssh_outer_timeout_above_short_http_probe():
    calls: list[dict] = []
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="jinan-aws",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is True
    assert calls[0]["timeout"] == runner.REMOTE_HEALTH_SSH_TIMEOUT_FLOOR
    assert "http://127.0.0.1:18792/healthz" in calls[0]["command"][-1]


def test_remote_http_ok_returns_false_on_outer_timeout():
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="jinan-aws",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is False


def test_local_remote_wrapper_kill_reap_is_bounded_and_drained(monkeypatch):
    release = threading.Event()
    consumer_started = threading.Event()
    consumer_finished = threading.Event()
    calls = []
    signals = []

    class StubbornProcess:
        pid = 424245

        def communicate(self, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            consumer_finished.set()
            return "", ""

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    quiesced = runner.terminate_local_process_group(
        StubbornProcess(),
        term_timeout=0.001,
        kill_timeout=0.001,
    )

    assert quiesced is False
    assert signals == [
        (424245, runner.signal.SIGTERM),
        (424245, runner.signal.SIGKILL),
    ]
    assert len(calls) >= 2
    assert all(0 <= timeout <= 0.001 for timeout in calls[:2])
    assert consumer_started.wait(timeout=0.2)
    release.set()
    assert consumer_finished.wait(timeout=0.2)


def test_remote_cleanup_kills_descendant_after_leader_exits(tmp_path):
    namespace = _remote_namespace(tmp_path)
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = namespace["terminate_process_group_bounded"](
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert namespace["process_group_exists"](process.pid) is False
    finally:
        try:
            namespace["os"].killpg(process.pid, namespace["signal"].SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_remote_normal_exit_cleans_residual_descendants(tmp_path):
    namespace = _remote_namespace(tmp_path)
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    try:
        assert process.wait(timeout=2) == 0
        assert namespace["process_group_exists"](process.pid) is True

        quiesced = namespace["ensure_process_group_quiesced_after_wait"](
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert namespace["process_group_exists"](process.pid) is False
    finally:
        try:
            namespace["os"].killpg(process.pid, namespace["signal"].SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_local_cleanup_kills_descendant_after_leader_exits(tmp_path):
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = runner.terminate_local_process_group(
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert runner._local_process_group_exists(process.pid) is False
    finally:
        try:
            runner.os.killpg(process.pid, runner.signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_local_normal_exit_cleans_residual_descendants(tmp_path):
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    try:
        assert process.wait(timeout=2) == 0
        assert runner._local_process_group_exists(process.pid) is True

        quiesced = runner._ensure_local_process_group_quiesced_after_wait(
            process,
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert runner._local_process_group_exists(process.pid) is False
    finally:
        try:
            runner.os.killpg(process.pid, runner.signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_local_remote_timeout_cleanup_re_raises_interrupt_after_drain(monkeypatch):
    calls = []
    signals = []

    class CooperativeProcess:
        pid = 424246

        def communicate(self, timeout=None):
            calls.append(timeout)
            return "", ""

    real_wait = runner._wait_for_owned_local_cleanup

    def interrupted_wait(done, *, timeout):
        completed, _interruption = real_wait(done, timeout=timeout)
        return completed, KeyboardInterrupt("caller cancelled during remote cleanup")

    monkeypatch.setattr(runner, "_wait_for_owned_local_cleanup", interrupted_wait)
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt, match="caller cancelled"):
        runner.terminate_local_process_group(
            CooperativeProcess(),
            term_timeout=0.01,
            kill_timeout=0.01,
        )

    assert signals == [(424246, runner.signal.SIGTERM)]
    assert len(calls) == 1
    assert 0 <= calls[0] <= 0.01


def test_run_remote_pending_system_exit_cleans_remote_and_local_process(
    monkeypatch,
    tmp_path,
):
    process_calls = []
    signals = []
    remote_cleanup_calls = []

    class SpawnedProcess:
        pid = 424254
        returncode = 0

        def communicate(self, input=None, timeout=None):
            process_calls.append((input, timeout))
            return "", ""

    process = SpawnedProcess()
    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: {"remote_proxy_base_url": "http://127.0.0.1:18788"},
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        runner,
        "terminate_remote_run",
        lambda **kwargs: remote_cleanup_calls.append(kwargs)
        or {"returncode": 0, "detail": {}},
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    real_restore = runner._restore_local_spawn_signals

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        raise SystemExit(80)

    monkeypatch.setattr(runner, "_restore_local_spawn_signals", restore_then_interrupt)
    args = SimpleNamespace(
        ssh_command="ssh",
        host="host",
        local_proxy_base_url="http://127.0.0.1:8878",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=True,
        no_sync_runtime=True,
        remote_runtime_repo="/remote/repo",
        proxy_env_file=tmp_path / "proxy.env",
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
        model_name="model",
        session_prefix="test",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=10,
    )

    with pytest.raises(SystemExit) as exc:
        runner.run_remote(args)

    assert exc.value.code == 80
    assert len(remote_cleanup_calls) == 1
    assert signals == [(424254, runner.signal.SIGTERM)]
    assert len(process_calls) == 1
    assert process_calls[0][0] is None
    assert 0 <= process_calls[0][1] <= runner.LOCAL_PROCESS_TERM_GRACE_SECONDS


def test_run_remote_normal_ssh_exit_cleanup_failure_is_technical(
    monkeypatch,
    tmp_path,
):
    class ReapedLeader:
        pid = 424264
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return json.dumps({"status": "done"}), ""

    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: {"remote_proxy_base_url": "http://127.0.0.1:18788"},
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pgid: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: (
            {
                "ok": False,
                "remote": {"returncode": 3},
                "local_cleanup_quiesced": False,
            },
            None,
        ),
    )
    args = SimpleNamespace(
        ssh_command="ssh",
        host="host",
        local_proxy_base_url="http://127.0.0.1:8878",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=True,
        no_sync_runtime=True,
        remote_runtime_repo="/remote/repo",
        proxy_env_file=tmp_path / "proxy.env",
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
        model_name="model",
        session_prefix="test",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=10,
    )

    with pytest.raises(RuntimeError, match="technical cleanup failure"):
        runner.run_remote(args)


def test_composite_cleanup_marks_remote_cleanup_failure(monkeypatch):
    class CooperativeProcess:
        pid = 424256

        def communicate(self, input=None, timeout=None):
            return "", ""

    monkeypatch.setattr(
        runner,
        "terminate_remote_run",
        lambda **kwargs: {
            "returncode": 3,
            "detail": {"ok": False, "status": "technical_cleanup_failed"},
        },
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *args, **kwargs: None)

    cleanup, interruption = runner._cleanup_remote_execution(
        ssh_command=["ssh"],
        host="host",
        base_run_dir="/remote/run",
        proc=CooperativeProcess(),
    )

    assert interruption is None
    assert cleanup["ok"] is False
    assert cleanup["remote"]["returncode"] == 3
    assert cleanup["local_cleanup_quiesced"] is True


def test_remote_owned_cleanup_defers_repeated_interrupts(tmp_path):
    namespace = _remote_namespace(tmp_path)

    class DoubleInterruptDone:
        calls = 0

        def is_set(self):
            return self.calls >= 3

        def wait(self, timeout):
            self.calls += 1
            if self.calls <= 2:
                raise KeyboardInterrupt(f"cancel-{self.calls}")
            return True

    completed, interruption = namespace["wait_for_owned_cleanup"](
        DoubleInterruptDone(),
        0.2,
    )

    assert completed is True
    assert isinstance(interruption, KeyboardInterrupt)
    assert interruption.args == ("cancel-1",)
