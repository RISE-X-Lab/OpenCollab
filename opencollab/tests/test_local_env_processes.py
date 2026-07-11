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
from pathlib import Path

import opencollab.adapters._env_local as local_mod
import opencollab.adapters._env_process as process_mod
import opencollab.adapters.env as env_mod
import pytest
from asyncio_test_support import assert_cancel_note, assert_cancel_reason
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
async def test_owned_operation_finishes_cleanup_then_rethrows_caller_cancel():
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def cleanup():
        await release.wait()
        cleaned.set()
        return "cleaned"

    owner = asyncio.create_task(
        env_mod._await_owned_operation(
            cleanup(),
            propagate_cancellation=True,
        )
    )
    await asyncio.sleep(0)
    owner.cancel("caller cancelled")
    await asyncio.sleep(0)
    assert owner.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await owner

    assert_cancel_reason(raised.value, "caller cancelled")
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_owned_transaction_preserves_caller_cancel_when_worker_self_cancels():
    started = asyncio.Event()
    release = asyncio.Event()

    async def self_cancelling_worker():
        started.set()
        await release.wait()
        raise asyncio.CancelledError("inner cancellation")

    owner = asyncio.create_task(
        env_mod._await_owned_transaction(
            self_cancelling_worker(),
            failure_note="owned test transaction",
        )
    )
    await started.wait()
    owner.cancel("caller cancellation")
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await owner

    assert_cancel_reason(raised.value, "caller cancellation")
    assert_cancel_note(
        raised.value,
        "owned test transaction failed after cancellation",
        "CancelledError",
    )


@pytest.mark.asyncio
async def test_local_timeout_kills_stubborn_grandchild_before_sentinel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(env_mod, "PROCESS_TERM_GRACE_SECONDS", 0.03)
    ready = tmp_path / "ready-timeout"
    sentinel = tmp_path / "sentinel-timeout"
    env = LocalEnvironment(str(tmp_path))

    result = await env.exec_cmd(
        _stubborn_grandchild_command(ready, sentinel, delay=1.0),
        timeout=0.5,
    )

    assert result.returncode == -1
    assert result.stdout == ""
    assert result.stderr == "Command timed out after 0.5s"
    assert ready.exists()
    await asyncio.sleep(0.6)
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

    assert_cancel_reason(captured.value, "first cancellation")
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

    env = LocalEnvironment(str(tmp_path))
    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", forbidden_spawn)

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

    def delayed_write(path, payload, root_fd=None):
        entered.set()
        assert release.wait(timeout=2)
        original_write(path, payload, root_fd)

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
    assert_cancel_reason(
        captured.value,
        "first cancellation while write is owned",
    )
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

    env = LocalEnvironment(str(tmp_path))
    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", spawn)
    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", lambda proc: False)

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

    env = LocalEnvironment(str(tmp_path))
    monkeypatch.setattr(env_mod, "_PROCESS_POPEN", spawn)
    monkeypatch.setattr(env_mod, "_sync_terminate_process_group", lambda proc: False)
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
    env = LocalEnvironment(str(tmp_path))

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
            env.exec_cmd(
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

    assert Path(owned_path).read_text(encoding="utf-8") == "foreign"
    assert not list(tmp_path.glob(".opencollab-retired-*"))


@pytest.mark.asyncio
async def test_local_temp_cleanup_is_idempotent_under_concurrent_remove(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    owned_path = await env.write_temp_file(
        "owned",
        prefix="opencollab-concurrent-",
        suffix=".tmp",
    )

    await asyncio.gather(env.remove_file(owned_path), env.remove_file(owned_path))

    assert not os.path.exists(owned_path)


@pytest.mark.asyncio
async def test_local_temp_creation_runs_off_loop_and_records_before_cancel(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    original_create = env_mod._sync_create_temp_file
    entered = threading.Event()
    release = threading.Event()
    created_path = None

    def delayed_create(temp_dir, prefix, suffix, payload):
        nonlocal created_path
        entered.set()
        assert release.wait(timeout=2)
        result = original_create(temp_dir, prefix, suffix, payload)
        created_path = result[0]
        return result

    monkeypatch.setattr(env_mod, "_sync_create_temp_file", delayed_create)
    env = LocalEnvironment(str(tmp_path))
    task = asyncio.create_task(
        env.write_temp_file("owned", prefix="opencollab-cancel-", suffix=".tmp")
    )
    assert await asyncio.to_thread(entered.wait, 0.5)

    await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
    assert task.done() is False
    task.cancel("cancel temp create")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created_path is not None
    assert created_path in env._temp_file_identities
    await env.remove_file(created_path)
    assert not os.path.exists(created_path)
    await env.cleanup()


@pytest.mark.asyncio
async def test_replaced_local_workspace_is_never_used_for_exec_or_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    owned_workspace = tmp_path / "owned-workspace"
    workspace.rename(owned_workspace)
    workspace.mkdir()

    with pytest.raises(OSError, match="workspace identity changed"):
        await env.exec_cmd("touch exec-victim")
    with pytest.raises(OSError, match="workspace identity changed"):
        await env.write_file("write-victim", "foreign overwrite")

    assert list(workspace.iterdir()) == []
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_validation_errors_do_not_acquire_workspace_duplicates(
    tmp_path,
    monkeypatch,
):
    env = LocalEnvironment(str(tmp_path))
    acquisitions = 0
    original_acquire = env._acquire_workspace_handle

    def counted_acquire():
        nonlocal acquisitions
        acquisitions += 1
        return original_acquire()

    monkeypatch.setattr(env, "_acquire_workspace_handle", counted_acquire)
    with pytest.raises(ValueError, match="path components"):
        await env.write_temp_file("data", prefix="bad/prefix")
    monkeypatch.setattr(env_mod, "LOCAL_FILE_WRITE_LIMIT_BYTES", 1)
    with pytest.raises(OSError, match="write limit"):
        await env.write_file("oversize", "too large")
    with pytest.raises(OSError, match="write limit"):
        await env.write_temp_file("too large", prefix="safe-")

    assert acquisitions == 0
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_cleanup_and_abort_release_workspace_and_temp_descriptors(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    temp_path = await env.write_temp_file("owned", prefix="cleanup-", suffix=".tmp")
    workspace_fd = env._workspace_fd
    temp_fd = env._temp_file_identities[temp_path].fd

    await env.cleanup()
    await env.cleanup()

    assert not os.path.exists(temp_path)
    assert env._temp_file_identities == {}
    with pytest.raises(OSError):
        os.fstat(workspace_fd)
    with pytest.raises(OSError):
        os.fstat(temp_fd)

    abort_workspace = tmp_path / "abort-workspace"
    abort_workspace.mkdir()
    aborted = LocalEnvironment(str(abort_workspace))
    abort_temp = await aborted.write_temp_file(
        "owned",
        prefix="abort-",
        suffix=".tmp",
    )
    abort_workspace_fd = aborted._workspace_fd
    abort_temp_fd = aborted._temp_file_identities[abort_temp].fd
    await aborted.abort()
    await aborted.abort()

    assert not os.path.exists(abort_temp)
    with pytest.raises(OSError):
        os.fstat(abort_workspace_fd)
    with pytest.raises(OSError):
        os.fstat(abort_temp_fd)


@pytest.mark.asyncio
async def test_local_abort_waits_for_active_command_group_before_returning(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    started = tmp_path / "started"
    late = tmp_path / "late"
    command = f"touch {shlex.quote(str(started))}; sleep 0.5; touch {shlex.quote(str(late))}"
    command_task = asyncio.create_task(env.exec_cmd(command, timeout=5))
    await _wait_for_file(started)

    await env.abort()
    result = await command_task

    assert result.returncode != 0
    assert not late.exists()
    await asyncio.sleep(0.6)
    assert not late.exists()
    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("touch should-not-run")


@pytest.mark.asyncio
async def test_local_cleanup_waits_for_active_command_group_before_returning(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    started = tmp_path / "cleanup-started"
    late = tmp_path / "cleanup-late"
    command = f"touch {shlex.quote(str(started))}; sleep 0.5; touch {shlex.quote(str(late))}"
    command_task = asyncio.create_task(env.exec_cmd(command, timeout=5))
    await _wait_for_file(started)

    await env.cleanup()
    result = await command_task

    assert result.returncode != 0
    await asyncio.sleep(0.6)
    assert not late.exists()


@pytest.mark.asyncio
async def test_revoked_process_operation_never_spawns_after_registry_attach(monkeypatch):
    registry = local_mod._ActiveProcessOperations()
    token = registry.begin()
    owner = process_mod._ThreadProcessOwner(
        "touch should-not-run",
        shell=True,
        cwd=None,
        cwd_fd=None,
        timeout=1.0,
        input_data=None,
        late_compensation=None,
    )

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("revoked command reached Popen")

    monkeypatch.setattr(process_mod, "_PROCESS_POPEN", forbidden_popen)
    abort_task = asyncio.create_task(registry.abort())
    await asyncio.sleep(0)
    registry.attach(token, owner)
    owner.start()
    registry.finish(token)

    await abort_task
    assert owner.finished.is_set()
    assert owner.result.returncode == -int(signal.SIGTERM)


@pytest.mark.asyncio
async def test_local_cleanup_recovers_when_process_owner_thread_cannot_start(
    monkeypatch,
    tmp_path,
):
    original_start = threading.Thread.start

    def fail_process_owner_start(thread):
        if thread.name.startswith("opencollab-process-owner-"):
            raise RuntimeError("thread unavailable")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_process_owner_start)
    env = LocalEnvironment(str(tmp_path))
    workspace_fd = env._workspace_fd

    with pytest.raises(RuntimeError, match="thread unavailable"):
        await env.exec_cmd("true")
    await env.cleanup()

    with pytest.raises(OSError):
        os.fstat(workspace_fd)
