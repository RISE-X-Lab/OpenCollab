"""Black-box checks for the compact local execution environment."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading

import pytest

from opencollab.adapters import _env_local as local_module
from opencollab.adapters import _env_process as process_module
from opencollab.adapters._env_process import ProcessCleanupError, run_process
from opencollab.adapters.env import LocalEnvironment


async def _wait_for(path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {path}")


def _descendant_command(ready, sentinel, *, delay: float = 0.35) -> str:
    child = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        f"time.sleep({delay});"
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    )
    parent = (
        "import subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "raise SystemExit(child.wait())"
    )
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(parent)}"


async def test_local_exec_returns_command_output(tmp_path) -> None:
    env = LocalEnvironment(str(tmp_path))
    result = await env.exec_cmd("printf 'hello'; printf 'problem' >&2")
    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == "problem"
    assert not result.stdout_truncated
    assert result.stdout_dropped_bytes == 0
    assert result.stderr_dropped_bytes == 0


async def test_local_exec_bounds_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(local_module, "PROCESS_OUTPUT_CAPTURE_BYTES", 32)
    env = LocalEnvironment(str(tmp_path))
    result = await env.exec_cmd(f"{shlex.quote(sys.executable)} -c \"print('x' * 100)\"")
    assert len(result.stdout.encode()) == 32
    assert result.stdout_truncated
    assert result.stdout_dropped_bytes == 69


@pytest.mark.parametrize(
    ("payload", "limit", "expected_stdout", "expected_dropped"),
    [
        (b"ok", 4, b"ok", 0),
        (b"four", 4, b"four", 0),
        (b"abcdef", 4, b"abef", 2),
    ],
    ids=("below-limit", "exact-limit", "over-limit"),
)
async def test_process_output_limit_counts_only_unretained_bytes(
    payload: bytes,
    limit: int,
    expected_stdout: bytes,
    expected_dropped: int,
) -> None:
    result = await run_process(
        (sys.executable, "-c", f"import os; os.write(1, {payload!r})"),
        shell=False,
        timeout=5,
        output_limit=limit,
    )

    assert result.stdout == expected_stdout
    assert result.stdout_dropped_bytes == expected_dropped
    assert result.to_exec_result().stdout_truncated is (expected_dropped > 0)


async def test_process_output_limit_preserves_real_head_and_tail() -> None:
    payload = b"HEAD" + b"x" * 100 + b"TAIL"

    result = await run_process(
        (sys.executable, "-c", f"import os; os.write(1, {payload!r})"),
        shell=False,
        timeout=5,
        output_limit=16,
    )

    assert result.stdout.startswith(b"HEAD")
    assert result.stdout.endswith(b"TAIL")
    assert len(result.stdout) == 16
    assert result.stdout_dropped_bytes == len(payload) - 16


async def test_local_timeout_kills_descendant_before_it_mutates_workspace(tmp_path) -> None:
    ready = tmp_path / "ready"
    sentinel = tmp_path / "late-write"
    # The timeout must fire *after* the descendant has started (written `ready`) but
    # *before* its deferred mutating write. An 80ms budget raced interpreter cold-start
    # — a double `python` spawn can take ~90ms, so the kill sometimes landed before
    # `ready` and the test hung on `_wait_for`. Give the timeout a wide margin over
    # cold-start and defer the mutating write well past it; the kill-before-write
    # guarantee does not need a tight window.
    late_write_delay = 1.0
    env = LocalEnvironment(str(tmp_path))
    owner = asyncio.create_task(
        env.exec_cmd(_descendant_command(ready, sentinel, delay=late_write_delay), timeout=0.5)
    )
    await _wait_for(ready)
    result = await owner
    assert result.returncode == -1
    # A surviving descendant would write at most timeout(0.5)+delay(1.0)=1.5s after exec
    # start; wait comfortably past that before asserting the write never landed.
    await asyncio.sleep(late_write_delay + 0.6)
    assert not sentinel.exists()


async def test_local_cancellation_kills_descendant_before_returning(tmp_path) -> None:
    ready = tmp_path / "ready"
    sentinel = tmp_path / "late-write"
    env = LocalEnvironment(str(tmp_path))
    owner = asyncio.create_task(env.exec_cmd(_descendant_command(ready, sentinel), timeout=5))
    await _wait_for(ready)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.sleep(0.4)
    assert not sentinel.exists()


async def test_local_double_cancellation_cannot_interrupt_process_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    ready = tmp_path / "ready"
    sentinel = tmp_path / "late-write"
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    original_terminate = process_module.terminate_process

    async def delayed_terminate(process, **kwargs):
        cleanup_started.set()
        await cleanup_release.wait()
        return await original_terminate(process, **kwargs)

    monkeypatch.setattr(process_module, "terminate_process", delayed_terminate)
    env = LocalEnvironment(str(tmp_path))
    owner = asyncio.create_task(env.exec_cmd(_descendant_command(ready, sentinel), timeout=5))
    await _wait_for(ready)
    owner.cancel()
    await cleanup_started.wait()
    owner.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.sleep(0.4)
    assert not sentinel.exists()


async def test_local_normal_exit_cleans_residual_descendant(tmp_path) -> None:
    sentinel = tmp_path / "late-write"
    child = f"import pathlib,time;time.sleep(0.3);pathlib.Path({str(sentinel)!r}).write_text('leaked')"
    parent = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    env = LocalEnvironment(str(tmp_path))

    result = await env.exec_cmd(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(parent)}")

    assert result.returncode == 0
    await asyncio.sleep(0.4)
    assert not sentinel.exists()


async def test_process_timeout_covers_blocked_stdin_writer(tmp_path) -> None:
    command = (sys.executable, "-c", "import time; time.sleep(5)")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_process(
                command,
                shell=False,
                cwd=str(tmp_path),
                timeout=0.05,
                input_bytes=b"x" * (2 * 1024 * 1024),
            ),
            timeout=2,
        )


async def test_process_timeout_covers_subprocess_spawn(monkeypatch, tmp_path) -> None:
    gate = asyncio.Event()
    terminated = asyncio.Event()
    original_spawn = process_module.asyncio.create_subprocess_exec
    original_terminate = process_module.terminate_process

    async def delayed_spawn(*args, **kwargs):
        await gate.wait()
        return await original_spawn(*args, **kwargs)

    async def tracked_terminate(process, **kwargs):
        result = await original_terminate(process, **kwargs)
        terminated.set()
        return result

    monkeypatch.setattr(
        process_module.asyncio,
        "create_subprocess_exec",
        delayed_spawn,
    )
    monkeypatch.setattr(process_module, "terminate_process", tracked_terminate)
    task = asyncio.create_task(
        run_process(
            (sys.executable, "-c", "import time; time.sleep(5)"),
            shell=False,
            cwd=str(tmp_path),
            timeout=0.01,
        )
    )

    try:
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(asyncio.TimeoutError):
            task.result()
    finally:
        gate.set()
        if not task.done():
            with pytest.raises(asyncio.TimeoutError):
                await task
        await asyncio.wait_for(terminated.wait(), timeout=1)


async def test_process_cancellation_during_spawn_is_bounded(
    monkeypatch,
    tmp_path,
) -> None:
    gate = asyncio.Event()
    terminated = asyncio.Event()
    original_spawn = process_module.asyncio.create_subprocess_exec
    original_terminate = process_module.terminate_process

    async def delayed_spawn(*args, **kwargs):
        await gate.wait()
        return await original_spawn(*args, **kwargs)

    async def tracked_terminate(process, **kwargs):
        result = await original_terminate(process, **kwargs)
        terminated.set()
        return result

    monkeypatch.setattr(
        process_module.asyncio,
        "create_subprocess_exec",
        delayed_spawn,
    )
    monkeypatch.setattr(process_module, "terminate_process", tracked_terminate)
    task = asyncio.create_task(
        run_process(
            (sys.executable, "-c", "import time; time.sleep(5)"),
            shell=False,
            cwd=str(tmp_path),
            timeout=5,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    try:
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            task.result()
    finally:
        gate.set()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(terminated.wait(), timeout=1)


async def test_registry_abort_bounds_unfinished_spawn_handoff(monkeypatch) -> None:
    registry = process_module.ProcessRegistry()
    spawn_started = asyncio.Event()
    spawn_release = asyncio.Event()
    process = object()

    async def factory():
        spawn_started.set()
        await spawn_release.wait()
        return process

    async def cleanup(candidate):
        assert candidate is process
        return True

    monkeypatch.setattr(process_module, "PROCESS_KILL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(process_module, "terminate_process", cleanup)
    spawn_owner = asyncio.create_task(registry.spawn(factory))
    await spawn_started.wait()
    abort_owner = asyncio.create_task(registry.abort())

    try:
        done, _pending = await asyncio.wait({abort_owner}, timeout=0.1)
        assert abort_owner in done
        with pytest.raises(ProcessCleanupError, match="spawn handoff"):
            abort_owner.result()
    finally:
        spawn_release.set()
        with pytest.raises(RuntimeError, match="revoked"):
            await spawn_owner
        if not abort_owner.done():
            await abort_owner

    await registry.abort()


async def test_cancellation_during_timeout_cleanup_is_preserved(monkeypatch) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_done = asyncio.Event()

    original_terminate = process_module.terminate_process

    async def delayed_cleanup(process, **kwargs):
        cleanup_started.set()
        await cleanup_release.wait()
        quiesced = await original_terminate(process, **kwargs)
        cleanup_done.set()
        return quiesced

    monkeypatch.setattr(process_module, "terminate_process", delayed_cleanup)
    owner = asyncio.create_task(
        run_process(
            (sys.executable, "-c", "import time; time.sleep(5)"),
            shell=False,
            timeout=0.01,
        )
    )
    await cleanup_started.wait()
    owner.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cleanup_done.is_set()


async def test_cancelled_spawn_reports_unproven_cleanup(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    process = object()

    async def factory():
        started.set()
        await release.wait()
        return process

    async def fail_cleanup(candidate):
        assert candidate is process
        return False

    monkeypatch.setattr(process_module, "terminate_process", fail_cleanup)
    owner = asyncio.create_task(process_module._spawn_owned(factory))
    await started.wait()
    owner.cancel()
    release.set()
    with pytest.raises(ProcessCleanupError, match="did not quiesce"):
        await owner


async def test_registry_abort_waits_for_spawn_handoff_cleanup(monkeypatch) -> None:
    registry = process_module.ProcessRegistry()
    spawn_started = asyncio.Event()
    spawn_release = asyncio.Event()
    cleanup_done = asyncio.Event()
    process = object()

    async def factory():
        spawn_started.set()
        await spawn_release.wait()
        return process

    async def cleanup(candidate):
        assert candidate is process
        cleanup_done.set()
        return True

    monkeypatch.setattr(process_module, "terminate_process", cleanup)
    spawn_owner = asyncio.create_task(registry.spawn(factory))
    await spawn_started.wait()
    abort_owner = asyncio.create_task(registry.abort())
    await asyncio.sleep(0)
    spawn_release.set()
    with pytest.raises(RuntimeError, match="revoked"):
        await spawn_owner
    await abort_owner
    assert cleanup_done.is_set()


async def test_cancelled_registry_registration_cleans_process_before_abort(
    monkeypatch,
) -> None:
    registry = process_module.ProcessRegistry()
    factory_started = asyncio.Event()
    factory_release = asyncio.Event()
    cleanup_done = asyncio.Event()
    process = object()

    async def factory():
        factory_started.set()
        await factory_release.wait()
        return process

    async def cleanup(candidate):
        assert candidate is process
        cleanup_done.set()
        return True

    monkeypatch.setattr(process_module, "terminate_process", cleanup)
    spawn_owner = asyncio.create_task(registry.spawn(factory))
    await factory_started.wait()
    await registry._condition.acquire()
    try:
        factory_release.set()
        await asyncio.sleep(0)
        spawn_owner.cancel()
        abort_owner = asyncio.create_task(registry.abort())
        await asyncio.sleep(0)
        assert abort_owner.done() is False
    finally:
        registry._condition.release()

    with pytest.raises(asyncio.CancelledError):
        await spawn_owner
    await abort_owner
    assert cleanup_done.is_set()


async def test_registry_retains_failed_cleanup_for_retry(monkeypatch) -> None:
    registry = process_module.ProcessRegistry()
    process = object()
    registry._processes.add(process)
    outcomes = iter((False, True))
    calls = 0

    async def cleanup(candidate):
        nonlocal calls
        assert candidate is process
        calls += 1
        return next(outcomes)

    monkeypatch.setattr(process_module, "terminate_process", cleanup)
    with pytest.raises(ProcessCleanupError, match="did not quiesce"):
        await registry.abort()
    assert process in registry._processes

    await registry.abort()
    assert process not in registry._processes
    assert calls == 2


async def test_run_process_retains_unquiesced_process_for_registry_retry(
    monkeypatch,
) -> None:
    registry = process_module.ProcessRegistry()
    original_terminate = process_module.terminate_process

    async def fail_cleanup(_process, **_kwargs):
        return False

    monkeypatch.setattr(process_module, "terminate_process", fail_cleanup)
    with pytest.raises(ProcessCleanupError, match="did not quiesce"):
        await run_process(
            (sys.executable, "-c", "import time; time.sleep(5)"),
            shell=False,
            timeout=0.01,
            registry=registry,
        )
    assert len(registry._processes) == 1

    monkeypatch.setattr(process_module, "terminate_process", original_terminate)
    await registry.abort()
    assert not registry._processes


async def test_local_file_operations_reject_symlink_and_escape(tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside)
    env = LocalEnvironment(str(tmp_path))
    with pytest.raises(OSError):
        await env.read_file("link")
    with pytest.raises(OSError):
        await env.write_file("link", "changed")
    with pytest.raises(PermissionError):
        await env.write_file("../escape", "changed")
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_local_file_io_does_not_block_event_loop(
    tmp_path,
    monkeypatch,
    operation,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_file_io(*_args, **_kwargs):
        started.set()
        assert release.wait(1.0)
        return b"payload"

    env = LocalEnvironment(str(tmp_path))
    if operation == "read":
        monkeypatch.setattr(local_module, "read_regular_bytes", slow_file_io)
        owner = asyncio.create_task(env.read_file("value"))
    else:
        monkeypatch.setattr(local_module, "write_regular_bytes_atomic", slow_file_io)
        owner = asyncio.create_task(env.write_file("value", "payload"))
    timer = threading.Timer(0.15, release.set)
    timer.start()
    try:
        await asyncio.sleep(0.02)
        assert started.is_set()
        assert not owner.done()
    finally:
        release.set()
        timer.cancel()
        await owner


async def test_local_cleanup_waits_for_cancelled_file_io(tmp_path, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_write(*_args, **_kwargs) -> None:
        started.set()
        assert release.wait(1.0)

    monkeypatch.setattr(local_module, "write_regular_bytes_atomic", slow_write)
    env = LocalEnvironment(str(tmp_path))
    writer = asyncio.create_task(env.write_file("value", "payload"))
    assert await asyncio.to_thread(started.wait, 1.0)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer

    cleanup = asyncio.create_task(env.cleanup())
    await asyncio.sleep(0.02)
    assert not cleanup.done()
    release.set()
    await cleanup


async def test_local_temp_files_have_owned_cleanup(tmp_path) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("payload", prefix="probe-", suffix=".txt")
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(OSError, match="unowned"):
        await env.remove_file(str(tmp_path / "foreign"))
    await env.remove_file(path)
    assert not os.path.exists(path)


async def test_local_temp_cleanup_removes_registered_path(tmp_path) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    await env.remove_file(path)
    assert not os.path.exists(path)


async def test_local_temp_cleanup_rejects_replaced_workspace_symlink(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    env = LocalEnvironment(str(workspace))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    saved_workspace = tmp_path / "saved-workspace"
    workspace.rename(saved_workspace)
    workspace.symlink_to(outside, target_is_directory=True)
    foreign = outside / os.path.basename(path)
    foreign.write_text("foreign", encoding="utf-8")

    with pytest.raises(OSError, match="real directory"):
        await env.remove_file(path)

    assert foreign.read_text(encoding="utf-8") == "foreign"


async def test_local_abort_blocks_future_operations_and_removes_temps(tmp_path) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("payload", prefix="probe-", suffix=".txt")
    await env.abort()
    assert not os.path.exists(path)
    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("true")


async def test_local_rejects_invalid_timeout_before_spawn(tmp_path) -> None:
    env = LocalEnvironment(str(tmp_path))
    with pytest.raises(ValueError, match="positive"):
        await env.exec_cmd("true", timeout=0)
