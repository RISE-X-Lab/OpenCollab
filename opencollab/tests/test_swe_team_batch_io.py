from __future__ import annotations

import fcntl
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import opencollab.adapters._atomic_rename as atomic_rename_mod
import pytest
from opencollab.adapters import safe_files as safe_files_mod

batch_io = importlib.import_module("scripts.swe_team_batch_io")


def _fake_swebench_package(tmp_path: Path) -> Path:
    package_root = tmp_path / "fake-packages"
    test_spec = package_root / "swebench" / "harness" / "test_spec"
    test_spec.mkdir(parents=True)
    for package in (
        package_root / "swebench",
        package_root / "swebench" / "harness",
        test_spec,
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "swebench" / "harness" / "utils.py").write_text(
        """
import json
import os


def load_swebench_dataset(dataset, split):
    return json.loads(os.environ["OC_TEST_DATASET"])
""",
        encoding="utf-8",
    )
    (test_spec / "test_spec.py").write_text(
        """
class _Spec:
    instance_image_key = "image:latest"


def make_test_spec(instance, namespace):
    return _Spec()
""",
        encoding="utf-8",
    )
    return package_root


@pytest.mark.parametrize(
    "instance_id",
    ["", ".", "..", "../escape", "nested/task", "task\trow", "task\u200drow"],
)
def test_batch_instance_id_rejects_path_and_tsv_injection(instance_id):
    with pytest.raises(ValueError):
        batch_io.validate_instance_id(instance_id)


def test_batch_prepare_rejects_symlinked_logs_dir_without_outside_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = tmp_path / "logs"
    logs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        batch_io.prepare_paths(tmp_path / "predictions.jsonl", logs)

    assert list(outside.iterdir()) == []


def test_batch_prepare_rejects_symlinked_output_parent_without_outside_write(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        batch_io.prepare_paths(linked / "predictions.jsonl", tmp_path / "logs")

    assert list(outside.iterdir()) == []


def test_batch_summary_append_rejects_final_symlink(tmp_path):
    _output, logs, summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    victim = tmp_path / "victim.tsv"
    victim.write_text("unchanged", encoding="utf-8")
    summary.unlink()
    summary.symlink_to(victim)

    with pytest.raises(OSError, match="regular"):
        batch_io.append_summary(
            summary,
            ["now", "task", "ok", "1", "2", "warn"],
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert logs.is_dir()


def test_batch_summary_lock_wait_is_bounded(tmp_path, monkeypatch):
    _output, _logs, summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    lock = summary.with_name(f".{summary.name}.lock")
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(batch_io, "LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring"):
            batch_io.append_summary(
                summary,
                ["now", "task", "ok", "1", "2", "warn"],
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_batch_log_rejects_symlink_without_starting_child(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    victim = tmp_path / "victim.log"
    victim.write_text("unchanged", encoding="utf-8")
    log = logs / "task.log"
    log.symlink_to(victim)
    sentinel = tmp_path / "started"

    with pytest.raises(OSError, match="regular or absent"):
        batch_io.run_with_log(
            log,
            [sys.executable, "-c", f"open({str(sentinel)!r}, 'w').write('x')"],
        )

    assert not sentinel.exists()
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_batch_log_captures_output_and_preserves_child_returncode(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    log = logs / "task.log"

    returncode = batch_io.run_with_log(
        log,
        [sys.executable, "-c", "import sys; print('captured'); raise SystemExit(7)"],
    )

    assert returncode == 7
    assert log.read_text(encoding="utf-8") == "captured\n"


def test_batch_atomic_write_rejects_swapped_temp_without_touching_victim(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "state.json"
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"foreign")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def swap_before_commit(source, destination, **kwargs):
        parent_fd = kwargs["src_dir_fd"]
        os.rename(
            source,
            f"{source}.detached",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.symlink(victim, source, dir_fd=parent_fd)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", swap_before_commit)

    with pytest.raises(OSError, match="changed during create"):
        batch_io.atomic_write(target, b"owned")

    assert target.is_symlink() and target.resolve() == victim
    assert victim.read_bytes() == b"foreign"
    detached = next(tmp_path.glob(".opencollab-retired-*.detached"))
    assert detached.read_bytes() == b"owned"


def test_batch_atomic_write_rejects_parent_swap_and_preserves_successor(
    tmp_path,
    monkeypatch,
):
    parent = tmp_path / "state"
    parent.mkdir()
    moved_parent = tmp_path / "state-moved"
    target = parent / "record.json"
    target.write_bytes(b"old")
    real_fsync = safe_files_mod.os.fsync
    swapped = False

    def swap_parent_after_payload_fsync(fd):
        nonlocal swapped
        result = real_fsync(fd)
        if not swapped and stat.S_ISREG(os.fstat(fd).st_mode):
            parent.rename(moved_parent)
            parent.mkdir()
            target.write_bytes(b"successor")
            swapped = True
        return result

    monkeypatch.setattr(safe_files_mod.os, "fsync", swap_parent_after_payload_fsync)

    with pytest.raises(OSError, match="parent changed before atomic replace"):
        batch_io.atomic_write(target, b"owned")

    assert swapped is True
    assert target.read_bytes() == b"successor"
    assert (moved_parent / "record.json").read_bytes() == b"old"
    assert list(moved_parent.glob(".oc-*.tmp")) == []


def test_batch_log_rejects_successor_replacement_after_child_start(
    tmp_path,
    monkeypatch,
):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    log = logs / "task.log"
    log.write_bytes(b"old")
    real_popen = batch_io.subprocess.Popen

    def replace_log_then_start(*args, **kwargs):
        log.unlink()
        log.write_bytes(b"successor")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(batch_io.subprocess, "Popen", replace_log_then_start)

    with pytest.raises(OSError, match="target identity changed before commit"):
        batch_io.run_with_log(
            log,
            [sys.executable, "-c", "print('must-not-publish')"],
        )

    assert log.read_bytes() == b"successor"
    assert list(logs.glob(".oc-*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
def test_batch_log_interrupt_kills_term_ignoring_child(tmp_path, monkeypatch):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    log = logs / "task.log"
    ready = tmp_path / "child.ready"
    real_popen = batch_io.subprocess.Popen
    child_pid: int | None = None

    class InterruptingProcess:
        def __init__(self, process):
            self._process = process
            self.pid = process.pid
            self._interrupted = False

        def wait(self, timeout=None):
            if not self._interrupted:
                self._interrupted = True
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert ready.exists()
                raise KeyboardInterrupt
            return self._process.wait(timeout=timeout)

    def start_interrupting_child(*args, **kwargs):
        nonlocal child_pid
        process = real_popen(*args, **kwargs)
        child_pid = process.pid
        return InterruptingProcess(process)

    monkeypatch.setattr(batch_io.subprocess, "Popen", start_interrupting_child)
    child_code = (
        "import os,pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )

    with pytest.raises(KeyboardInterrupt):
        batch_io.run_with_log(log, [sys.executable, "-c", child_code])

    assert child_pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not log.exists()
    assert list(logs.glob(".oc-*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
def test_batch_log_normal_parent_exit_kills_lingering_descendant(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    log = logs / "task.log"
    ready = tmp_path / "descendant.ready"
    descendant_code = (
        "import os,pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        f"ready=pathlib.Path({str(ready)!r});"
        f"subprocess.Popen([sys.executable,'-c',{descendant_code!r}]);"
        "deadline=time.monotonic()+2;"
        "\nwhile not ready.exists() and time.monotonic()<deadline: time.sleep(0.01);"
        "\nassert ready.exists();"
        "\nprint('parent exited')"
    )

    returncode = batch_io.run_with_log(
        log,
        [sys.executable, "-c", parent_code],
    )

    descendant_pid = int(ready.read_text(encoding="utf-8"))
    assert returncode == 0
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)
    assert log.read_text(encoding="utf-8") == "parent exited\n"


def test_batch_script_checks_missing_start_and_has_column_fallback():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    ).read_text(encoding="utf-8")

    assert "--start-from instance was not found" in source
    assert "if command -v column" in source
    assert "display-summary --summary" in source
    assert "flock(fd, fcntl.LOCK_EX)" not in source


def test_batch_script_rejects_missing_start_from_in_dataset(tmp_path):
    package_root = _fake_swebench_package(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    env = os.environ.copy()
    env.update(
        {
            "OPENCOLLAB_EVAL_PYTHON": sys.executable,
            "OPENCOLLAB_SWEBENCH_NAMESPACE": "swebench",
            "PYTHONPATH": str(package_root),
            "OC_TEST_DATASET": json.dumps(
                [{"instance_id": "demo__task-1", "problem_statement": "fix"}]
            ),
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--logs-dir",
            str(tmp_path / "logs"),
            "--start-from",
            "demo__missing",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "--start-from instance was not found" in result.stderr


def test_batch_script_rejects_dataset_id_with_tsv_control(tmp_path):
    package_root = _fake_swebench_package(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_team_batch.sh"
    env = os.environ.copy()
    env.update(
        {
            "OPENCOLLAB_EVAL_PYTHON": sys.executable,
            "PYTHONPATH": str(package_root),
            "OC_TEST_DATASET": json.dumps(
                [{"instance_id": "demo\ttask", "problem_statement": "fix"}]
            ),
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--logs-dir",
            str(tmp_path / "logs"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert not list((tmp_path / "logs").glob("*.instance.json"))


def test_batch_io_cli_run_log_returns_child_status(tmp_path):
    _output, logs, _summary = batch_io.prepare_paths(
        tmp_path / "predictions.jsonl",
        tmp_path / "logs",
    )
    script = Path(batch_io.__file__)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "run-log",
            "--log",
            str(logs / "cli.log"),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(9)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 9
