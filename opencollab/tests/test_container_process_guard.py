from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "container_process_guard.sh"
TEAM_RUNNER = REPO_ROOT / "scripts" / "start_team_run.sh"


def _load_guard_module():
    path = REPO_ROOT / "scripts" / "container_process_guard.py"
    spec = importlib.util.spec_from_file_location("container_process_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _term_ignoring_child(
    started: Path,
    finished: Path,
    *,
    delay: float = 0.6,
    pidfile: Path | None = None,
) -> str:
    record_pid = (
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        if pidfile is not None
        else ""
    )
    code = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        + record_pid
        + f"pathlib.Path({str(started)!r}).touch(); "
        + f"time.sleep({delay}); "
        + f"pathlib.Path({str(finished)!r}).touch()"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_container_guard_stop_kills_term_ignoring_descendant(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_pidfile = tmp_path / "child.pid"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text(
        "#!/bin/sh\n"
        'if [ -n "${PGREP_PID_FILE:-}" ] && [ -s "$PGREP_PID_FILE" ]; then\n'
        '  pid=$(cat "$PGREP_PID_FILE")\n'
        '  if kill -0 "$pid" 2>/dev/null; then echo "$pid"; exit 0; fi\n'
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_pgrep.chmod(0o755)
    fake_ps = fake_bin / "ps"
    fake_ps.write_text(
        "#!/bin/sh\n"
        'pid=$(cat "$PGREP_PID_FILE" 2>/dev/null || true)\n'
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$pid S"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PGREP_PID_FILE"] = str(child_pidfile)
    command = (
        f"{_term_ignoring_child(started, finished, delay=2.0, pidfile=child_pidfile)} & wait"
    )
    runner = subprocess.Popen(
        ["bash", str(GUARD), "run", str(pidfile), str(cancelfile), "bash", "-lc", command],
        env=env,
    )
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()
    probe = subprocess.run(
        ["pgrep", "-s", "0"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert child_pidfile.read_text(encoding="utf-8") in probe.stdout

    stopped = subprocess.run(
        ["bash", str(GUARD), "stop", str(pidfile), str(cancelfile)],
        check=False,
        env=env,
        timeout=5,
    )

    assert stopped.returncode == 0
    runner.wait(timeout=5)
    time.sleep(2.2)
    assert not finished.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_container_guard_cancel_marker_blocks_late_start(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    sentinel = tmp_path / "ran"

    stopped = subprocess.run(
        ["bash", str(GUARD), "stop", str(pidfile), str(cancelfile)],
        check=False,
        timeout=5,
    )
    late = subprocess.run(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            f'touch "{sentinel}"',
        ],
        check=False,
        timeout=5,
    )

    assert stopped.returncode == 0
    assert late.returncode == 125
    assert not sentinel.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions")
def test_container_guard_enumeration_failure_is_fail_closed(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    sentinel = tmp_path / "descendant-finished"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_pgrep.chmod(0o755)
    fake_ps = fake_bin / "ps"
    fake_ps.write_text(
        "#!/bin/sh\n"
        "pid=''\n"
        "previous=''\n"
        "for value in \"$@\"; do\n"
        '  if [ "$previous" = "-p" ]; then pid="$value"; break; fi\n'
        "  previous=\"$value\"\n"
        "done\n"
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$pid S"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    child = _term_ignoring_child(sentinel.with_suffix(".started"), sentinel, delay=0.5)
    runner = subprocess.Popen(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            f"set -m; {child} & wait",
        ],
        env=env,
    )
    for _ in range(100):
        if pidfile.exists():
            break
        time.sleep(0.01)
    assert pidfile.exists()

    stopped = subprocess.run(
        ["bash", str(GUARD), "stop", str(pidfile), str(cancelfile)],
        check=False,
        env=env,
        timeout=5,
    )

    assert stopped.returncode == 125
    assert pidfile.exists()
    assert runner.wait(timeout=5) == 125
    time.sleep(0.6)
    assert sentinel.exists()
    assert pidfile.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions")
def test_container_guard_duplicate_run_cannot_replace_owner(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    second_ran = tmp_path / "second-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text(
        "#!/bin/sh\n"
        'sid="$2"\n'
        'if kill -0 "$sid" 2>/dev/null; then echo "$sid"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_pgrep.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    first = subprocess.Popen(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            "sleep 0.5; exit 7",
        ],
        env=env,
    )
    for _ in range(100):
        if pidfile.exists():
            break
        time.sleep(0.01)
    assert pidfile.exists()
    first_marker = pidfile.read_text(encoding="utf-8")

    second = subprocess.run(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            f'touch "{second_ran}"',
        ],
        check=False,
        env=env,
        timeout=5,
    )

    assert second.returncode == 125
    assert pidfile.read_text(encoding="utf-8") == first_marker
    assert not second_ran.exists()
    assert first.wait(timeout=5) == 7
    assert not pidfile.exists()


def test_container_guard_stop_after_clean_nonzero_exit_is_idempotent(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text(
        "#!/bin/sh\n"
        'sid="$2"\n'
        'if kill -0 "$sid" 2>/dev/null; then echo "$sid"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_pgrep.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    exited = subprocess.run(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            "exit 7",
        ],
        check=False,
        env=env,
        timeout=5,
    )
    stopped = subprocess.run(
        ["bash", str(GUARD), "stop", str(pidfile), str(cancelfile)],
        check=False,
        env=env,
        timeout=5,
    )

    assert exited.returncode == 7
    assert stopped.returncode == 0
    assert not pidfile.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.parametrize("kind", ["pidfile", "lock"])
def test_container_guard_fifo_marker_race_fails_quickly(tmp_path, kind):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    target = pidfile if kind == "pidfile" else Path(str(pidfile) + ".lock")
    os.mkfifo(target)

    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(GUARD), "prepare", str(pidfile), str(cancelfile)],
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert time.monotonic() - started < 1.0
    assert result.returncode == 125
    assert "regular file" in result.stderr


def test_container_guard_rejects_symlinked_marker_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            "bash",
            str(GUARD),
            "prepare",
            str(linked / "run.pid"),
            str(linked / "run.cancel"),
        ],
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 125
    assert list(outside.iterdir()) == []


def test_container_guard_record_read_detects_post_open_replacement(
    tmp_path,
    monkeypatch,
):
    guard = _load_guard_module()
    marker = tmp_path / "run.pid"
    payload = {
        "schema": "opencollab.container-process.v1",
        "session_id": 12345,
        "owner_pid": 12346,
        "start_identity": "identity",
        "nonce": "a" * 32,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    original_read = guard.os.read
    swapped = False

    def swap_after_read(fd, size):
        nonlocal swapped
        data = original_read(fd, size)
        if data and not swapped:
            swapped = True
            os.replace(replacement, marker)
        return data

    monkeypatch.setattr(guard.os, "read", swap_after_read)

    with pytest.raises(guard.GuardError, match="changed while reading"):
        guard._read_record(marker)


def test_container_guard_owned_record_cleanup_fails_on_corrupt_replacement(tmp_path):
    guard = _load_guard_module()
    marker = tmp_path / "run.pid"
    marker.write_text("{corrupt", encoding="utf-8")
    expected = {
        "schema": "opencollab.container-process.v1",
        "session_id": 12345,
        "owner_pid": 12346,
        "start_identity": "identity",
        "nonce": "a" * 32,
    }

    with pytest.raises(guard.GuardError, match="malformed"):
        guard._remove_owned_record(marker, expected)

    assert marker.read_text(encoding="utf-8") == "{corrupt"


def test_container_guard_pgrep_results_filter_zombies(monkeypatch):
    guard = _load_guard_module()
    monkeypatch.setattr(
        guard.shutil,
        "which",
        lambda name: f"/{name}",
    )
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="11\n12\n"),
            SimpleNamespace(returncode=0, stdout="11 Z\n12 S\n"),
        ]
    )
    monkeypatch.setattr(guard.subprocess, "run", lambda *args, **kwargs: next(results))

    assert guard._command_session_members(777) == {12}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_container_guard_signal_cleans_owned_session(tmp_path):
    pidfile = tmp_path / "run.pid"
    cancelfile = tmp_path / "run.cancel"
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text(
        "#!/bin/sh\n"
        'sid="$2"\n'
        'if kill -0 "$sid" 2>/dev/null; then echo "$sid"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_pgrep.chmod(0o755)
    fake_ps = fake_bin / "ps"
    fake_ps.write_text(
        "#!/bin/sh\n"
        "pid=''\n"
        "previous=''\n"
        "for value in \"$@\"; do\n"
        '  if [ "$previous" = "-p" ]; then pid="$value"; break; fi\n'
        "  previous=\"$value\"\n"
        "done\n"
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$pid S"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    runner = subprocess.Popen(
        [
            "bash",
            str(GUARD),
            "run",
            str(pidfile),
            str(cancelfile),
            "bash",
            "-lc",
            _term_ignoring_child(started, finished, delay=2.0),
        ],
        env=env,
    )
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()

    runner.terminate()

    assert runner.wait(timeout=5) == 143
    time.sleep(2.2)
    assert not finished.exists()
    assert not pidfile.exists()


def test_team_runner_uses_guard_bounded_diff_and_locked_append():
    source = TEAM_RUNNER.read_text(encoding="utf-8")

    assert "container_process_guard.sh" in source
    assert "bounded_container_output_command" in source
    assert "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" in source
    assert '>> "$output"' not in source
