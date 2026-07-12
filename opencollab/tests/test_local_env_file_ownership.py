"""Atomic local-file replacement and temporary ownership regressions."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import opencollab.adapters._atomic_rename as atomic_rename_mod
import opencollab.adapters._owned_file_cleanup as owned_cleanup_mod
import opencollab.adapters.env as env_mod
import pytest
from opencollab.adapters.env import LocalEnvironment


@pytest.mark.asyncio
async def test_local_atomic_write_preserves_foreign_target_after_temp_swap(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("old", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("victim", encoding="utf-8")
    real_rename_exchange = atomic_rename_mod.rename_exchange
    exchanged = False

    def swap_temp_before_commit(source, destination, **kwargs):
        nonlocal exchanged
        if (
            not exchanged
            and str(source).startswith(owned_cleanup_mod.RETIRED_FILE_PREFIX)
            and destination == target.name
        ):
            parent_fd = kwargs["first_dir_fd"]
            os.rename(
                source,
                f"{source}.detached",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.symlink(victim, source, dir_fd=parent_fd)
            exchanged = True
        return real_rename_exchange(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_exchange", swap_temp_before_commit)
    env = LocalEnvironment(str(workspace))

    with pytest.raises(OSError, match="changed during replace"):
        await env.write_file("target.txt", "new")

    assert exchanged is True
    assert victim.read_text(encoding="utf-8") == "victim"
    assert target.is_symlink() and target.resolve() == victim
    retired = list(workspace.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert any(entry.is_file() and not entry.is_symlink() and entry.read_text() == "old" for entry in retired)
    detached = list(workspace.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*.detached"))
    assert len(detached) == 1 and detached[0].read_text() == "new"
    await env.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_exists", [False, True])
async def test_local_atomic_write_refuses_concurrent_successor(
    tmp_path,
    monkeypatch,
    initial_exists,
):
    target = tmp_path / "target.txt"
    if initial_exists:
        target.write_text("old", encoding="utf-8")
    real_fsync = env_mod.os.fsync
    injected = False

    def install_successor_after_temp_sync(fd):
        nonlocal injected
        result = real_fsync(fd)
        if not injected and stat.S_ISREG(os.fstat(fd).st_mode):
            successor = tmp_path / "successor.txt"
            successor.write_text("successor", encoding="utf-8")
            os.replace(successor, target)
            injected = True
        return result

    monkeypatch.setattr(env_mod.os, "fsync", install_successor_after_temp_sync)
    env = LocalEnvironment(str(tmp_path))

    expected = "changed" if initial_exists else "appeared"
    with pytest.raises(OSError, match=rf"local file {expected} before atomic replace"):
        await env.write_file("target.txt", "writer")

    assert injected is True
    assert target.read_text(encoding="utf-8") == "successor"
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_atomic_write_preserves_successor_in_final_noreplace_window(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "target.txt"
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    injected = False

    def install_successor_before_commit(source, destination, **kwargs):
        nonlocal injected
        if (
            not injected
            and str(source).startswith(owned_cleanup_mod.RETIRED_FILE_PREFIX)
            and destination == target.name
        ):
            target.write_text("successor", encoding="utf-8")
            injected = True
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", install_successor_before_commit)
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(OSError, match="local file appeared before atomic replace"):
        await env.write_file("target.txt", "candidate")

    assert injected is True
    assert target.read_text(encoding="utf-8") == "successor"
    retired_payloads = [
        entry.read_text(encoding="utf-8")
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    ]
    assert retired_payloads == ["candidate"]
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_atomic_write_restores_old_target_after_directory_sync_failure(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    real_fsync = env_mod.os.fsync
    directory_syncs = 0

    def fail_commit_directory_sync(fd):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("commit directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(env_mod.os, "fsync", fail_commit_directory_sync)
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(OSError, match="commit directory fsync failed"):
        await env.write_file("target.txt", "new")

    assert target.read_text(encoding="utf-8") == "old"
    retired_payloads = sorted(
        entry.read_text(encoding="utf-8")
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )
    assert retired_payloads == ["new"]
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_atomic_write_preserves_existing_mode(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    env = LocalEnvironment(str(tmp_path))

    await env.write_file("target.txt", "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_atomic_write_rejects_reserved_retirement_destination(tmp_path):
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(ValueError, match="reserved retirement namespace"):
        await env.write_file(".opencollab-retired-user", "payload")

    assert not (tmp_path / ".opencollab-retired-user").exists()
    await env.cleanup()


@pytest.mark.skipif(sys.platform != "darwin", reason="verified macOS root alias")
def test_local_absolute_tmp_alias_supports_write_and_read():
    alias_directory = Path(tempfile.mkdtemp(prefix="opencollab-alias-", dir="/tmp"))
    canonical_directory = Path(os.path.realpath(alias_directory))
    alias_target = alias_directory / "value.bin"
    try:
        env_mod._sync_write_regular_file(str(alias_target), b"payload")

        assert env_mod._sync_read_regular_file(str(alias_target), 64) == b"payload"
        assert (canonical_directory / "value.bin").read_bytes() == b"payload"
    finally:
        (canonical_directory / "value.bin").unlink(missing_ok=True)
        canonical_directory.rmdir()


@pytest.mark.skipif(sys.platform != "darwin", reason="verified macOS root alias")
def test_local_absolute_tmp_alias_still_rejects_descendant_symlink():
    alias_directory = Path(tempfile.mkdtemp(prefix="opencollab-alias-", dir="/tmp"))
    canonical_directory = Path(os.path.realpath(alias_directory))
    victim = canonical_directory / "victim"
    victim.mkdir()
    link = canonical_directory / "linked"
    link.symlink_to(victim, target_is_directory=True)
    try:
        with pytest.raises(
            OSError,
            match="not a real directory|Not a directory|symbolic link",
        ):
            env_mod._sync_write_regular_file(
                str(alias_directory / link.name / "escaped.bin"),
                b"payload",
            )

        assert not (victim / "escaped.bin").exists()
    finally:
        link.unlink()
        victim.rmdir()
        canonical_directory.rmdir()


@pytest.mark.asyncio
async def test_local_remove_rejects_unknown_path_without_touching_victim(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("preserve", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))

    with pytest.raises(OSError, match="without temporary ownership proof"):
        await env.remove_file("victim.txt")

    assert victim.read_text(encoding="utf-8") == "preserve"
    await env.cleanup()


@pytest.mark.asyncio
async def test_local_temp_removal_retires_owned_inode_without_unlink(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(env_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    env = LocalEnvironment(str(tmp_path))
    owned_path = await env.write_temp_file(
        "owned",
        prefix="opencollab-quarantine-",
        suffix=".tmp",
    )
    monkeypatch.setattr(
        owned_cleanup_mod.os,
        "unlink",
        lambda *_args, **_kwargs: pytest.fail("retirement must not unlink by name"),
        raising=False,
    )

    await env.remove_file(owned_path)

    assert not os.path.exists(owned_path)
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 1
    assert retired[0].read_text(encoding="utf-8") == "owned"
    await env.cleanup()
