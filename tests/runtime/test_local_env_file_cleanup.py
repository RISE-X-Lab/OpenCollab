"""Workspace-descriptor cleanup regressions for ``LocalEnvironment``."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from opencollab.adapters import _env_local as local_module
from opencollab.adapters import safe_anchored_files
from opencollab.adapters.env import LocalEnvironment


async def test_local_cleanup_retains_workspace_fd_for_temp_removal_retry(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    workspace_fd = env._workspace_fd
    real_removal = local_module.unlink_regular_file_durable_at

    def fail_removal(*_args, **_kwargs):
        raise OSError("forced removal failure")

    monkeypatch.setattr(
        local_module,
        "unlink_regular_file_durable_at",
        fail_removal,
    )

    with pytest.raises(OSError, match="failed to remove"):
        await env.cleanup()

    assert env._workspace_fd == workspace_fd
    os.fstat(workspace_fd)
    assert os.path.exists(path)

    monkeypatch.setattr(
        local_module,
        "unlink_regular_file_durable_at",
        real_removal,
    )
    await env.cleanup()

    assert not os.path.exists(path)
    assert env._workspace_fd is None
    with pytest.raises(OSError):
        os.fstat(workspace_fd)


async def test_local_cleanup_removes_files_after_process_abort_failure(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    workspace_fd = env._workspace_fd

    async def fail_abort() -> None:
        raise local_module.ProcessCleanupError("forced process cleanup failure")

    monkeypatch.setattr(env._processes, "abort", fail_abort)

    with pytest.raises(local_module.ProcessCleanupError, match="forced process"):
        await env.cleanup()

    assert not os.path.exists(path)
    assert env._workspace_fd is None
    with pytest.raises(OSError):
        os.fstat(workspace_fd)


async def test_local_cleanup_removes_files_when_process_abort_is_cancelled(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    workspace_fd = env._workspace_fd
    abort_started = asyncio.Event()

    async def delayed_abort() -> None:
        abort_started.set()
        await asyncio.Future()

    monkeypatch.setattr(env._processes, "abort", delayed_abort)
    cleanup = asyncio.create_task(env.cleanup())
    await abort_started.wait()
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert not os.path.exists(path)
    assert env._workspace_fd is None
    with pytest.raises(OSError):
        os.fstat(workspace_fd)


async def test_local_cleanup_reports_process_and_file_failures(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    await env.write_temp_file("owned", prefix="probe-", suffix=".txt")

    async def fail_abort() -> None:
        raise local_module.ProcessCleanupError("forced process cleanup failure")

    def fail_removal(*_args, **_kwargs):
        raise OSError("forced removal failure")

    monkeypatch.setattr(env._processes, "abort", fail_abort)
    monkeypatch.setattr(
        local_module,
        "unlink_regular_file_durable_at",
        fail_removal,
    )

    with pytest.raises(
        local_module.ProcessCleanupError,
        match="forced process cleanup failure",
    ) as captured:
        await env.cleanup()

    assert any(
        "file cleanup also failed" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    assert isinstance(captured.value.__cause__, OSError)


async def test_local_cleanup_cancellation_still_closes_workspace_fd(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    path = await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    workspace_fd = env._workspace_fd
    removal_started = threading.Event()
    release_removal = threading.Event()
    real_removal = local_module.unlink_regular_file_durable_at

    def delayed_removal(*args, **kwargs):
        removal_started.set()
        assert release_removal.wait(timeout=2)
        return real_removal(*args, **kwargs)

    monkeypatch.setattr(
        local_module,
        "unlink_regular_file_durable_at",
        delayed_removal,
    )
    cleanup = asyncio.create_task(env.cleanup())
    assert await asyncio.to_thread(removal_started.wait, 2)

    cleanup.cancel()
    await asyncio.sleep(0)
    assert not cleanup.done()
    release_removal.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert not os.path.exists(path)
    assert env._workspace_fd is None
    with pytest.raises(OSError):
        os.fstat(workspace_fd)


def test_atomic_write_closes_parent_fd_when_temporary_unlink_fails(
    tmp_path,
    monkeypatch,
) -> None:
    root_fd = safe_anchored_files.open_directory_anchor(str(tmp_path))
    closed: list[int] = []
    parent_fds: list[int] = []
    real_close = safe_anchored_files.os.close
    real_unlink = safe_anchored_files.os.unlink

    def fail_temporary_unlink(path, *args, **kwargs):
        if str(path).startswith(".state."):
            parent_fds.append(kwargs["dir_fd"])
            raise PermissionError("forced temporary unlink failure")
        return real_unlink(path, *args, **kwargs)

    def record_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(safe_anchored_files.os, "unlink", fail_temporary_unlink)
    monkeypatch.setattr(safe_anchored_files.os, "close", record_close)
    try:
        with pytest.raises(PermissionError, match="temporary unlink"):
            safe_anchored_files.write_regular_bytes_atomic_at(
                root_fd,
                str(tmp_path),
                "state",
                b"payload",
            )
    finally:
        real_close(root_fd)

    assert parent_fds
    assert parent_fds[0] in closed
