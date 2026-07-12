"""Durable unlink and retirement-registry reclamation regressions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import opencollab.adapters._atomic_rename as atomic_rename_mod
import opencollab.adapters._owned_file_cleanup as owned_cleanup_mod
import opencollab.adapters.retirement_registry as retirement_registry_mod
import opencollab.adapters.safe_files as safe_files_mod
import pytest
from opencollab.adapters.storage import SessionStore


def test_durable_unlink_rejects_stale_target_identity(tmp_path):
    target = tmp_path / "owner.json"
    target.write_bytes(b"current")
    current = target.stat()

    with pytest.raises(OSError, match="target identity changed"):
        safe_files_mod.unlink_regular_file_durable(
            target,
            expected_target_identity=(current.st_dev, current.st_ino + 1),
        )

    assert target.read_bytes() == b"current"


def test_durable_unlink_retires_owned_inode_without_any_unlink(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"owned")
    identity = target.stat()
    monkeypatch.setattr(
        owned_cleanup_mod.os,
        "unlink",
        lambda *_args, **_kwargs: pytest.fail("retirement must not unlink by name"),
        raising=False,
    )

    removed = safe_files_mod.unlink_regular_file_durable(
        target,
        expected_target_identity=(identity.st_dev, identity.st_ino),
    )

    assert removed is True
    assert not target.exists()
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 1
    assert retired[0].read_bytes() == b"owned"


def test_retirement_destination_final_window_never_clobbers_foreign_inode(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"owned")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    stolen_name = None
    stolen_identity = None

    def steal_first_destination(source, destination, **kwargs):
        nonlocal stolen_name, stolen_identity
        if source == target.name and stolen_name is None:
            parent_fd = kwargs["dst_dir_fd"]
            foreign_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.write(foreign_fd, b"foreign")
            foreign = os.fstat(foreign_fd)
            os.close(foreign_fd)
            stolen_name = destination
            stolen_identity = (foreign.st_dev, foreign.st_ino)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        steal_first_destination,
    )

    assert safe_files_mod.unlink_regular_file_durable(target) is True

    assert stolen_name is not None and stolen_identity is not None
    stolen = tmp_path / stolen_name
    current = stolen.stat()
    assert (current.st_dev, current.st_ino) == stolen_identity
    assert current.st_nlink == 1
    assert stolen.read_bytes() == b"foreign"
    assert any(
        entry.read_bytes() == b"owned"
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )


def test_durable_unlink_refuses_entry_swapped_after_open_without_harming_victim(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"owned")
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"foreign")
    detached = tmp_path / "detached.json"
    real_rename = os.rename
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    injected = False

    def exchange_before_quarantine(source, destination, **kwargs):
        nonlocal injected
        if not injected and source == target.name:
            injected = True
            parent_fd = kwargs["src_dir_fd"]
            real_rename(
                source,
                detached.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.link(victim, source, dst_dir_fd=parent_fd)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        exchange_before_quarantine,
    )

    with pytest.raises(OSError, match="retired entry does not match owned file"):
        safe_files_mod.unlink_regular_file_durable(target)

    assert victim.read_bytes() == b"foreign"
    assert target.read_bytes() == b"foreign"
    assert target.stat().st_ino == victim.stat().st_ino
    assert detached.read_bytes() == b"owned"
    assert not list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))


def test_durable_unlink_refuses_retirement_count_overflow(tmp_path, monkeypatch):
    target = tmp_path / "owner.json"
    target.write_bytes(b"owned")
    (tmp_path / f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}existing").write_bytes(b"old")
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", 1)

    with pytest.raises(OSError, match="retired-file count limit"):
        safe_files_mod.unlink_regular_file_durable(target)

    assert target.read_bytes() == b"owned"


def test_durable_unlink_refuses_retirement_byte_overflow(tmp_path, monkeypatch):
    target = tmp_path / "owner.json"
    target.write_bytes(b"owned")
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_BYTES_PER_DIRECTORY", 1)

    with pytest.raises(OSError, match="retired-file byte limit"):
        safe_files_mod.unlink_regular_file_durable(target)

    assert target.read_bytes() == b"owned"


def test_session_store_reclaims_verified_tombstones_and_compacts_registry(
    tmp_path,
    monkeypatch,
):
    retirement_log = tmp_path / "retirements.jsonl"
    retirement_log.touch()
    monkeypatch.setenv(
        retirement_registry_mod.INTERNAL_RETIREMENT_LOG_ENV,
        str(retirement_log),
    )
    monkeypatch.setenv(
        retirement_registry_mod.INTERNAL_RETIREMENT_WORKSPACE_ENV,
        str(tmp_path),
    )
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", 4)
    target = tmp_path / "session.json"
    store = SessionStore()

    for index in range(20):
        store.save(str(target), [{"role": "user", "content": str(index)}])

    assert store.load_messages(str(target), "fallback") == [
        {"role": "user", "content": "19"}
    ]
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) <= 3
    assert len(retirement_log.read_bytes().splitlines()) == len(retired)
    with retirement_registry_mod._lock:
        retirement_registry_mod._records.clear()
    assert set(retirement_registry_mod.registered_retirement_paths(tmp_path)) == {
        path.name for path in retired
    }


def test_persistent_registry_survives_process_restarts_past_directory_limit(
    tmp_path,
):
    target = tmp_path / "shared.json"
    target.write_bytes(b"initial")
    foreign = tmp_path / f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}foreign"
    foreign.write_bytes(b"foreign")
    registry = retirement_registry_mod.initialize_persistent_retirement_log(
        tmp_path / "retirements.jsonl"
    )
    package_root = Path(__file__).resolve().parents[1]
    code = """
import sys
from pathlib import Path
from opencollab.adapters.safe_files import write_regular_bytes_atomic

target = Path(sys.argv[1])
start = int(sys.argv[2])
for index in range(start, start + 70):
    write_regular_bytes_atomic(target, str(index).encode())
"""
    environment = os.environ.copy()
    environment[retirement_registry_mod.INTERNAL_RETIREMENT_LOG_ENV] = registry
    environment.pop(retirement_registry_mod.INTERNAL_RETIREMENT_WORKSPACE_ENV, None)
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(package_root), environment.get("PYTHONPATH", ""))
        if value
    )

    for start in range(0, 280, 70):
        subprocess.run(
            [sys.executable, "-c", code, str(target), str(start)],
            check=True,
            env=environment,
        )

    assert target.read_bytes() == b"279"
    assert foreign.read_bytes() == b"foreign"
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert foreign in retired
    assert len(retired) <= owned_cleanup_mod.MAX_RETIRED_FILES_PER_DIRECTORY
    assert len(Path(registry).read_bytes().splitlines()) <= len(retired) - 1


def test_registry_rewrite_crash_keeps_previous_registry_readable(tmp_path):
    target = tmp_path / "shared.json"
    target.write_bytes(b"initial")
    registry = retirement_registry_mod.initialize_persistent_retirement_log(
        tmp_path / "retirements.jsonl"
    )
    package_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment[retirement_registry_mod.INTERNAL_RETIREMENT_LOG_ENV] = registry
    environment.pop(retirement_registry_mod.INTERNAL_RETIREMENT_WORKSPACE_ENV, None)
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(package_root), environment.get("PYTHONPATH", ""))
        if value
    )
    normal_code = """
import sys
from pathlib import Path
from opencollab.adapters.safe_files import write_regular_bytes_atomic
write_regular_bytes_atomic(Path(sys.argv[1]), sys.argv[2].encode())
"""
    subprocess.run(
        [sys.executable, "-c", normal_code, str(target), "before-crash"],
        check=True,
        env=environment,
    )
    valid_before = Path(registry).read_bytes()
    crash_code = """
import os
import sys
from pathlib import Path
from opencollab.adapters import retirement_registry
from opencollab.adapters.safe_files import write_regular_bytes_atomic

def crash_mid_rewrite(parent_fd, name, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    os.write(fd, payload[:max(1, len(payload) // 2)])
    os.fsync(fd)
    os._exit(73)

retirement_registry._write_registry_payload = crash_mid_rewrite
write_regular_bytes_atomic(Path(sys.argv[1]), b"crashing-write")
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code, str(target)],
        check=False,
        env=environment,
    )

    assert crashed.returncode == 73
    assert Path(registry).read_bytes() == valid_before
    retirement_registry_mod.initialize_persistent_retirement_log(registry)
    assert not list(tmp_path.glob(".opencollab-retirement-registry-*.tmp"))
    subprocess.run(
        [sys.executable, "-c", normal_code, str(target), "after-crash"],
        check=True,
        env=environment,
    )
    assert target.read_bytes() == b"after-crash"

    invalid = tmp_path / "invalid-retirements.jsonl"
    invalid.write_bytes(b"{}\n")
    with pytest.raises(OSError, match="malformed"):
        retirement_registry_mod.initialize_persistent_retirement_log(invalid)


def test_retirement_reclamation_preserves_unregistered_reserved_entries(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "session.json"
    store = SessionStore()
    store.save(str(target), [{"role": "user", "content": "initial"}])
    foreign = [
        tmp_path / f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}foreign-{index}"
        for index in range(2)
    ]
    for path in foreign:
        path.write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", 2)

    with pytest.raises(OSError, match="retired-file count limit"):
        store.save(str(target), [{"role": "user", "content": "replacement"}])

    assert target.exists()
    assert all(path.read_text(encoding="utf-8") == "foreign" for path in foreign)


def test_repeated_durable_unlink_reclaims_verified_tombstones(tmp_path, monkeypatch):
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", 4)

    for index in range(20):
        target = tmp_path / f"owned-{index}.json"
        target.write_text(str(index), encoding="utf-8")
        assert safe_files_mod.unlink_regular_file_durable(target) is True
        assert not target.exists()

    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 4
    assert set(retirement_registry_mod.registered_retirement_paths(tmp_path)) == {
        path.name for path in retired
    }
