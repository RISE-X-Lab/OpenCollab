"""Process-ownership regressions for ``LocalEnvironment``."""

from __future__ import annotations

import asyncio
import io
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

import opencollab.adapters.env as env_mod
from opencollab.adapters.env import LocalEnvironment


def _stubborn_grandchild_command(ready, sentinel, *, delay: float = 0.4) -> str:
    code = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        f"time.sleep({delay}); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} & wait"


def _closed_pipe_descendant_command(ready, sentinel, *, delay: float = 0.4) -> str:
    code = (
        "import os, pathlib, signal, time; "
        "os.close(1); os.close(2); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        f"time.sleep({delay}); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} &"


async def _wait_for_file(path) -> None:
    while not path.exists():
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_local_timeout_kills_stubborn_grandchild_before_sentinel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.03)
    ready = tmp_path / "ready-timeout"
    sentinel = tmp_path / "sentinel-timeout"
    env = LocalEnvironment(str(tmp_path))

    result = await env.exec_cmd(
        _stubborn_grandchild_command(ready, sentinel),
        timeout=0.2,
    )

    assert result.returncode == -1
    assert result.stdout == ""
    assert result.stderr == "Command timed out after 0.2s"
    assert ready.exists()
    await asyncio.sleep(0.3)
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_local_caller_cancel_kills_stubborn_grandchild_before_sentinel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.03)
    ready = tmp_path / "ready-cancel"
    sentinel = tmp_path / "sentinel-cancel"
    env = LocalEnvironment(str(tmp_path))
    task = asyncio.create_task(
        env.exec_cmd(_stubborn_grandchild_command(ready, sentinel), timeout=60)
    )
    await asyncio.wait_for(_wait_for_file(ready), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.45)
    assert not sentinel.exists()

    followup = await env.exec_cmd("printf reusable", timeout=1)
    assert followup.returncode == 0
    assert followup.stdout == "reusable"


@pytest.mark.asyncio
async def test_local_double_cancel_cannot_interrupt_term_kill_and_reap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.08)
    ready = tmp_path / "ready-double-cancel"
    sentinel = tmp_path / "sentinel-double-cancel"
    term_sent = threading.Event()
    real_signal_process_group = env_mod._sync_signal_process_group

    def observed_signal(proc, sig):
        result = real_signal_process_group(proc, sig)
        if sig is signal.SIGTERM:
            term_sent.set()
        return result

    monkeypatch.setattr(env_mod, "_sync_signal_process_group", observed_signal)
    env = LocalEnvironment(str(tmp_path))
    task = asyncio.create_task(
        env.exec_cmd(
            _stubborn_grandchild_command(ready, sentinel, delay=0.45),
            timeout=60,
        )
    )
    await asyncio.wait_for(_wait_for_file(ready), timeout=1.0)

    task.cancel("first cancellation")
    assert await asyncio.to_thread(term_sent.wait, 0.5)
    task.cancel("second cancellation during TERM grace")
    with pytest.raises(asyncio.CancelledError) as captured:
        await asyncio.wait_for(task, timeout=1.0)

    assert captured.value.args == ("first cancellation",)
    assert task.cancelled() is True
    await asyncio.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_local_reaps_closed_pipe_descendant_after_leader_exit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.03)
    ready = tmp_path / "ready-closed-pipes"
    sentinel = tmp_path / "sentinel-closed-pipes"
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(OSError, match="descendants remained alive"):
        await env.exec_cmd(
            _closed_pipe_descendant_command(ready, sentinel),
            timeout=2,
        )

    await asyncio.sleep(0.45)
    assert not sentinel.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf")],
)
async def test_local_invalid_timeout_never_spawns(
    tmp_path, monkeypatch, invalid_timeout
):
    spawns = 0

    def forbidden_spawn(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        raise AssertionError("invalid timeout reached Popen")

    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", forbidden_spawn)
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(ValueError, match="positive finite"):
        await env.exec_cmd("echo side-effect", timeout=invalid_timeout)

    assert spawns == 0


@pytest.mark.asyncio
async def test_local_large_output_is_bounded_and_reports_dropped_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_OUTPUT_CAPTURE_BYTES", 4096)
    payload_size = 200_000
    code = (
        "import sys; "
        f"sys.stdout.write('A' * {payload_size}); "
        f"sys.stderr.write('B' * {payload_size})"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    result = await LocalEnvironment(str(tmp_path)).exec_cmd(command, timeout=5)

    assert result.returncode == 0
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert result.stdout_dropped_bytes == payload_size - 4096
    assert result.stderr_dropped_bytes == payload_size - 4096
    assert "opencollab truncated" in result.stdout
    assert "opencollab truncated" in result.stderr
    assert len(result.stdout.encode()) < 4300
    assert len(result.stderr.encode()) < 4300


@pytest.mark.asyncio
async def test_local_read_file_rejects_content_above_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "LOCAL_FILE_READ_LIMIT_BYTES", 32)
    (tmp_path / "large.bin").write_bytes(b"x" * 33)

    with pytest.raises(OSError, match="exceeds read limit"):
        await LocalEnvironment(str(tmp_path)).read_file("large.bin")


@pytest.mark.asyncio
async def test_local_write_file_rejects_utf8_payload_above_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "LOCAL_FILE_WRITE_LIMIT_BYTES", 4)

    with pytest.raises(OSError, match="exceeds write limit"):
        await LocalEnvironment(str(tmp_path)).write_file("large.txt", "€€")

    assert not (tmp_path / "large.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
async def test_local_read_file_rejects_fifo_without_blocking_event_loop(tmp_path):
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    heartbeat = asyncio.create_task(asyncio.sleep(0))

    with pytest.raises(OSError):
        await asyncio.wait_for(
            LocalEnvironment(str(tmp_path)).read_file(fifo.name),
            timeout=1,
        )

    await asyncio.wait_for(heartbeat, timeout=0.2)
    regular = tmp_path / "regular.txt"
    regular.write_text("usable", encoding="utf-8")
    assert await LocalEnvironment(str(tmp_path)).read_file(regular.name) == "usable"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
async def test_local_write_file_rejects_fifo_without_blocking_event_loop(tmp_path):
    fifo = tmp_path / "output.fifo"
    os.mkfifo(fifo)
    heartbeat = asyncio.create_task(asyncio.sleep(0))

    with pytest.raises(OSError):
        await asyncio.wait_for(
            LocalEnvironment(str(tmp_path)).write_file(fifo.name, "payload"),
            timeout=1,
        )

    await asyncio.wait_for(heartbeat, timeout=0.2)
    assert stat.S_ISFIFO(fifo.stat().st_mode)


@pytest.mark.asyncio
async def test_local_read_and_write_reject_device_files():
    env = LocalEnvironment()

    with pytest.raises(OSError, match="non-regular"):
        await asyncio.wait_for(env.read_file(os.devnull), timeout=1)
    with pytest.raises(OSError, match="non-regular"):
        await asyncio.wait_for(env.write_file(os.devnull, "payload"), timeout=1)


@pytest.mark.asyncio
async def test_local_read_and_write_reject_final_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(victim)
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(OSError):
        await env.read_file(link.name)
    with pytest.raises(OSError):
        await env.write_file(link.name, "changed")

    assert victim.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
async def test_local_read_rejects_ancestor_replaced_by_symlink_after_check(tmp_path):
    workspace = tmp_path / "workspace"
    checked_parent = workspace / "checked"
    checked_parent.mkdir(parents=True)
    checked_file = checked_parent / "data.txt"
    checked_file.write_text("original", encoding="utf-8")
    safe_path = os.path.realpath(checked_file)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("secret", encoding="utf-8")
    checked_parent.rename(workspace / "checked-old")
    checked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        await LocalEnvironment(str(workspace)).read_file(safe_path)


@pytest.mark.asyncio
async def test_local_write_rejects_ancestor_symlink_without_touching_victim(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    (workspace / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        await LocalEnvironment(str(workspace)).write_file(
            "redirect/victim.txt",
            "changed",
        )

    assert victim.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_cancelled_local_write_waits_for_owned_worker(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_write = env_mod._sync_write_regular_file

    def delayed_write(path, payload):
        entered.set()
        assert release.wait(timeout=2)
        original_write(path, payload)

    monkeypatch.setattr(env_mod, "_sync_write_regular_file", delayed_write)
    env = LocalEnvironment(str(tmp_path))
    task = asyncio.create_task(env.write_file("owned.txt", "complete"))
    assert await asyncio.to_thread(entered.wait, 0.5)

    task.cancel("first cancellation while write is owned")
    await asyncio.sleep(0.02)
    assert task.done() is False
    task.cancel("second cancellation while write is owned")
    await asyncio.sleep(0.02)
    assert task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await asyncio.wait_for(task, timeout=1)
    assert captured.value.args == ("first cancellation while write is owned",)
    assert (tmp_path / "owned.txt").read_text(encoding="utf-8") == "complete"


class _ExplodingPipe:
    def read(self, _size):
        raise OSError("pipe transport failed")

    def close(self):
        return None


class _HungProcessWithBrokenPipe:
    pid = 49173
    stdin = None
    stdout = _ExplodingPipe()
    stderr = io.BytesIO()

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("broken", timeout)

    def terminate(self):
        return None

    def kill(self):
        return None


class _StableHungProcess:
    pid = 49174
    stdin = None

    def __init__(self):
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("hung", timeout)

    def terminate(self):
        return None

    def kill(self):
        return None


@pytest.mark.asyncio
async def test_local_transport_error_plus_cleanup_failure_revokes_environment(
    tmp_path, monkeypatch
):
    spawns = 0

    def spawn(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        return _HungProcessWithBrokenPipe()

    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", spawn)
    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", lambda proc: False)
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(env_mod._OwnedProcessNotQuiesced) as captured:
        await env.exec_cmd("broken", timeout=1)
    assert isinstance(captured.value.__cause__, OSError)

    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("must not spawn", timeout=1)
    assert spawns == 1


@pytest.mark.asyncio
async def test_local_cancel_cleanup_failure_revokes_environment(tmp_path, monkeypatch):
    spawned = threading.Event()
    spawns = 0

    def spawn(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        spawned.set()
        return _StableHungProcess()

    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", spawn)
    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", lambda proc: False)
    env = LocalEnvironment(str(tmp_path))
    task = asyncio.create_task(env.exec_cmd("hung", timeout=60))
    assert await asyncio.to_thread(spawned.wait, 0.5)

    task.cancel("cleanup cannot quiesce")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() is True

    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("must not spawn", timeout=1)
    assert spawns == 1


def test_cancel_at_spawn_boundary_survives_event_loop_close(tmp_path, monkeypatch):
    ready = tmp_path / "ready-late-spawn"
    sentinel = tmp_path / "sentinel-late-spawn"
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    original_popen = env_mod._PROCESS_POPEN

    def delayed_popen(*args, **kwargs):
        spawn_entered.set()
        release_spawn.wait(timeout=2)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", delayed_popen)
    monkeypatch.setattr(env_mod, "PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(env_mod, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(env_mod, "PROCESS_IO_JOIN_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(
            LocalEnvironment(str(tmp_path)).exec_cmd(
                _stubborn_grandchild_command(ready, sentinel),
                timeout=60,
            )
        )
        assert await asyncio.to_thread(spawn_entered.wait, 0.5)
        task.cancel("cancel before Popen returns")
        threading.Timer(0.25, release_spawn.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    release_spawn.set()
    deadline = time.monotonic() + 1.0
    while any(
        thread.name.startswith("opencollab-process-owner-")
        for thread in threading.enumerate()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(
        thread.name.startswith("opencollab-process-owner-")
        for thread in threading.enumerate()
    )
    time.sleep(0.45)
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_local_temp_file_respects_abort_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    await env.abort()

    with pytest.raises(RuntimeError, match="aborted"):
        await env.write_temp_file(
            "must not exist",
            prefix="opencollab-aborted-",
            suffix=".tmp",
        )

    assert list(tmp_path.glob("opencollab-aborted-*")) == []


@pytest.mark.asyncio
async def test_local_temp_file_skips_preplaced_symlink_and_uses_mode_0600(
    tmp_path,
    monkeypatch,
):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    collision = tmp_path / "opencollab-guard-collision.patch"
    collision.symlink_to(victim)
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        env_mod.tempfile,
        "_get_candidate_names",
        lambda: iter(("collision", "owned")),
    )
    env = LocalEnvironment(str(tmp_path))

    owned_path = await env.write_temp_file(
        "private patch",
        prefix="opencollab-guard-",
        suffix=".patch",
    )

    assert owned_path == str(tmp_path / "opencollab-guard-owned.patch")
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert (tmp_path / "opencollab-guard-owned.patch").read_text() == "private patch"
    assert stat.S_IMODE((tmp_path / "opencollab-guard-owned.patch").stat().st_mode) == 0o600
    await env.remove_file(owned_path)
    assert not (tmp_path / "opencollab-guard-owned.patch").exists()


@pytest.mark.asyncio
async def test_local_temp_cleanup_refuses_replaced_foreign_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    owned_path = await env.write_temp_file(
        "owned",
        prefix="opencollab-owner-",
        suffix=".tmp",
    )
    os.unlink(owned_path)
    with open(owned_path, "w", encoding="utf-8") as replacement:
        replacement.write("foreign")

    with pytest.raises(OSError, match="replaced local temporary file"):
        await env.remove_file(owned_path)

    assert open(owned_path, encoding="utf-8").read() == "foreign"
