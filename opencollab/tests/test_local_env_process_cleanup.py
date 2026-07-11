"""Resource cleanup regressions for ``LocalEnvironment``."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import stat
import threading
from pathlib import Path

import opencollab.adapters._env_local as local_mod
import opencollab.adapters._env_process as process_mod
import opencollab.adapters.env as env_mod
import pytest
from opencollab.adapters.env import LocalEnvironment


async def _wait_for_file(path) -> None:
    while not path.exists():
        await asyncio.sleep(0.005)


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
