"""Retry regressions for ``LocalEnvironment`` cleanup."""

import os

import opencollab.adapters.env as env_mod
import pytest
from opencollab.adapters.env import LocalEnvironment


@pytest.mark.asyncio
async def test_local_cleanup_retains_retryable_temp_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    temp_path = await env.write_temp_file("owned", prefix="retry-", suffix=".tmp")
    workspace_fd = env._workspace_fd
    temp_fd = env._temp_file_identities[temp_path].fd
    original_unlink = env_mod._sync_unlink_file
    failed = False

    def fail_once(path, identity, root_fd=None):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("transient unlink failure")
        return original_unlink(path, identity, root_fd)

    monkeypatch.setattr(env_mod, "_sync_unlink_file", fail_once)
    with pytest.raises(OSError, match="transient unlink failure"):
        await env.cleanup()

    assert temp_path in env._temp_file_identities
    os.fstat(workspace_fd)
    os.fstat(temp_fd)

    await env.cleanup()
    assert not os.path.exists(temp_path)
    with pytest.raises(OSError):
        os.fstat(workspace_fd)
    with pytest.raises(OSError):
        os.fstat(temp_fd)
