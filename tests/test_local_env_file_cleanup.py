"""Workspace-descriptor cleanup regressions for ``LocalEnvironment``."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from opencollab.adapters import _env_local as local_module
from opencollab.adapters.env import LocalEnvironment


async def test_local_cleanup_closes_workspace_fd_when_temp_removal_fails(
    tmp_path,
    monkeypatch,
) -> None:
    env = LocalEnvironment(str(tmp_path))
    await env.write_temp_file("owned", prefix="probe-", suffix=".txt")
    workspace_fd = env._workspace_fd

    def fail_removal(*_args, **_kwargs):
        raise OSError("forced removal failure")

    monkeypatch.setattr(
        local_module,
        "unlink_regular_file_durable_at",
        fail_removal,
    )

    with pytest.raises(OSError, match="failed to remove"):
        await env.cleanup()

    assert env._workspace_fd is None
    with pytest.raises(OSError):
        os.fstat(workspace_fd)


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
