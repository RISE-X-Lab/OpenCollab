"""CAS and quarantine regressions for reusable safe-file helpers."""

from __future__ import annotations

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import opencollab.adapters._atomic_file_commit as atomic_commit_mod
import opencollab.adapters._atomic_rename as atomic_rename_mod
import opencollab.adapters._owned_file_cleanup as owned_cleanup_mod
import opencollab.adapters.retirement_registry as retirement_registry_mod
import opencollab.adapters.safe_files as safe_files_mod
import pytest


def test_atomic_rename_rejects_embedded_nul(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="one directory entry"):
            atomic_rename_mod.rename_noreplace(
                "source\0suffix",
                "destination",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
    finally:
        os.close(parent_fd)


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


@pytest.mark.parametrize("initial_exists", [False, True])
def test_atomic_write_cas_refuses_successor_installed_during_parent_verification(
    tmp_path,
    monkeypatch,
    initial_exists,
):
    target = tmp_path / "owner.json"
    expected_identity = None
    if initial_exists:
        target.write_bytes(b"old")
        opened = target.stat()
        expected_identity = (opened.st_dev, opened.st_ino)
    original_verify = safe_files_mod._verify_atomic_parent_binding
    injected = False

    def install_successor(*args, **kwargs):
        nonlocal injected
        result = original_verify(*args, **kwargs)
        if not injected and kwargs["phase"] == "before":
            if target.exists():
                target.unlink()
            target.write_bytes(b"successor")
            injected = True
        return result

    monkeypatch.setattr(safe_files_mod, "_verify_atomic_parent_binding", install_successor)
    kwargs = (
        {"expected_target_identity": expected_identity}
        if initial_exists
        else {"require_target_absent": True}
    )

    with pytest.raises(OSError, match="target (?:identity changed|appeared) before commit"):
        safe_files_mod.write_regular_bytes_atomic(target, b"writer", **kwargs)

    assert injected is True
    assert target.read_bytes() == b"successor"


def test_require_absent_preserves_successor_in_final_noreplace_window(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    injected = False

    def install_successor_before_rename(source, destination, **kwargs):
        nonlocal injected
        if not injected and destination == target.name:
            target.write_bytes(b"successor")
            injected = True
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        install_successor_before_rename,
    )

    with pytest.raises(FileExistsError) as raised:
        safe_files_mod.write_regular_bytes_atomic(
            target,
            b"candidate",
            require_target_absent=True,
        )

    assert injected is True
    assert target.read_bytes() == b"successor"
    assert any(
        "concurrent successor preserved at atomic target" in note
        for note in raised.value.__notes__
    )
    retired_payloads = [
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    ]
    assert retired_payloads == [b"candidate"]


def test_require_absent_preserves_successor_replacing_committed_candidate(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    successor_source = tmp_path / "successor.json"
    successor_source.write_bytes(b"successor")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    real_stat = atomic_commit_mod.os.stat
    real_replace = atomic_commit_mod.os.replace
    committed = False
    injected = False

    def track_commit(source, destination, **kwargs):
        nonlocal committed
        result = real_rename_noreplace(source, destination, **kwargs)
        committed = True
        return result

    def replace_candidate_before_postcheck(path, *args, **kwargs):
        nonlocal injected
        name = os.fsdecode(path) if isinstance(path, (str, bytes)) else ""
        if committed and not injected and name == target.name and kwargs.get("dir_fd") is not None:
            parent_fd = kwargs["dir_fd"]
            os.rename(
                target.name,
                f"{target.name}.candidate",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            real_replace(successor_source, target)
            injected = True
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", track_commit)
    monkeypatch.setattr(atomic_commit_mod.os, "stat", replace_candidate_before_postcheck)

    with pytest.raises(OSError, match="changed during create") as raised:
        safe_files_mod.write_regular_bytes_atomic(
            target,
            b"candidate",
            require_target_absent=True,
        )

    assert injected is True
    assert target.read_bytes() == b"successor"
    assert (tmp_path / "owner.json.candidate").read_bytes() == b"candidate"
    assert any(
        "concurrent successor preserved at atomic target" in note
        for note in raised.value.__notes__
    )


def test_atomic_write_restores_previous_target_after_commit_sync_failure(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_fsync = safe_files_mod.os.fsync
    calls = 0

    def fail_commit_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("commit directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(safe_files_mod.os, "fsync", fail_commit_sync)

    with pytest.raises(OSError, match="commit directory fsync failed"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    retired_payloads = {
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    }
    assert retired_payloads == {b"old", b"new"}


def test_atomic_write_restores_verified_backup_when_retirement_sync_fails(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_fsync = safe_files_mod.os.fsync
    failed = False

    def fail_first_directory_sync(fd):
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("backup retirement fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(safe_files_mod.os, "fsync", fail_first_directory_sync)

    with pytest.raises(OSError, match="backup retirement fsync failed"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert failed is True
    assert target.read_bytes() == b"old"
    retired_payloads = sorted(
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )
    assert retired_payloads == [b"new", b"old"]


def test_atomic_recovery_reads_pinned_backup_after_tombstone_name_swap(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_fsync = safe_files_mod.os.fsync
    directory_syncs = 0

    def swap_backup_name_then_fail_commit(fd):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                backup = next(
                    tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
                )
                stolen = tmp_path / "stolen-backup"
                backup.rename(stolen)
                backup.write_bytes(b"foreign")
                raise OSError("commit directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(safe_files_mod.os, "fsync", swap_backup_name_then_fail_commit)

    with pytest.raises(OSError, match="commit directory fsync failed"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert (tmp_path / "stolen-backup").read_bytes() == b"old"
    assert any(
        entry.read_bytes() == b"foreign"
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )


def test_successful_commit_survives_backup_close_reporting_after_close(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_open_regular_at = atomic_commit_mod._open_regular_at
    real_close = atomic_commit_mod.os.close
    backup_fd = None
    injected = False

    def capture_backup_fd(*args, **kwargs):
        nonlocal backup_fd
        result = real_open_regular_at(*args, **kwargs)
        if result is not None:
            backup_fd = result[0]
        return result

    def close_then_report_failure(fd):
        nonlocal injected
        if fd == backup_fd and not injected:
            injected = True
            real_close(fd)
            raise OSError("close reported after closing backup")
        return real_close(fd)

    monkeypatch.setattr(atomic_commit_mod, "_open_regular_at", capture_backup_fd)
    monkeypatch.setattr(atomic_commit_mod.os, "close", close_then_report_failure)

    with pytest.raises(OSError, match="close reported after closing backup"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert injected is True
    assert target.read_bytes() == b"new"
    assert any(
        entry.read_bytes() == b"old"
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )


def test_backup_close_error_never_closes_reused_descriptor(tmp_path, monkeypatch):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"victim")
    real_open_regular_at = atomic_commit_mod._open_regular_at
    real_close = atomic_commit_mod.os.close
    backup_fd = None
    replacement_fd = None
    injected = False

    def capture_backup_fd(*args, **kwargs):
        nonlocal backup_fd
        result = real_open_regular_at(*args, **kwargs)
        if result is not None:
            backup_fd = result[0]
        return result

    def close_reuse_then_report_failure(fd):
        nonlocal injected, replacement_fd
        if fd == backup_fd and not injected:
            injected = True
            real_close(fd)
            replacement_fd = os.open(victim, os.O_RDONLY)
            assert replacement_fd == fd
            raise OSError("backup close failed after descriptor reuse")
        return real_close(fd)

    monkeypatch.setattr(atomic_commit_mod, "_open_regular_at", capture_backup_fd)
    monkeypatch.setattr(atomic_commit_mod.os, "close", close_reuse_then_report_failure)

    with pytest.raises(OSError, match="backup close failed after descriptor reuse"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert injected is True
    assert replacement_fd is not None
    assert os.fstat(replacement_fd).st_size == len(b"victim")
    assert target.read_bytes() == b"new"
    real_close(replacement_fd)


@pytest.mark.parametrize(
    ("count_limit", "byte_limit"),
    [(2, 1024), (10, 6)],
)
def test_atomic_write_reserves_full_failure_recovery_capacity(
    tmp_path,
    monkeypatch,
    count_limit,
    byte_limit,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def rename_then_fail(source, destination, **kwargs):
        result = real_rename_noreplace(source, destination, **kwargs)
        if destination == target.name:
            raise OSError("commit hook failed")
        return result

    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", count_limit)
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_BYTES_PER_DIRECTORY", byte_limit)
    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", rename_then_fail)

    with pytest.raises(OSError, match="commit hook failed"):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    retired_payloads = sorted(
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )
    assert retired_payloads == [b"new", b"old"]


@pytest.mark.parametrize(
    ("count_limit", "byte_limit", "message", "expected_retired"),
    [
        (1, 1024, "count limit", []),
        (10, 5, "byte limit", []),
    ],
)
def test_atomic_write_refuses_commit_without_full_recovery_capacity(
    tmp_path,
    monkeypatch,
    count_limit,
    byte_limit,
    message,
    expected_retired,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", count_limit)
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_BYTES_PER_DIRECTORY", byte_limit)

    with pytest.raises(OSError, match=message):
        safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    retired_payloads = [
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    ]
    assert retired_payloads == expected_retired


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


def test_existing_target_final_successor_is_never_overwritten(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    successor_identity = None

    def install_successor_before_commit(source, destination, **kwargs):
        nonlocal successor_identity
        if (
            successor_identity is None
            and source.startswith(owned_cleanup_mod.RETIRED_FILE_PREFIX)
            and destination == target.name
        ):
            target.write_bytes(b"successor")
            opened = target.stat()
            successor_identity = (opened.st_dev, opened.st_ino)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(
        atomic_rename_mod,
        "rename_noreplace",
        install_successor_before_commit,
    )

    with pytest.raises(FileExistsError) as raised:
        safe_files_mod.write_regular_bytes_atomic(target, b"candidate")

    assert successor_identity is not None
    current = target.stat()
    assert (current.st_dev, current.st_ino) == successor_identity
    assert current.st_nlink == 1
    assert target.read_bytes() == b"successor"
    assert any(
        "concurrent successor preserved at atomic target" in note
        for note in raised.value.__notes__
    )
    retired_payloads = sorted(
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    )
    assert retired_payloads == [b"candidate", b"old"]


def test_existing_target_preserves_successor_installed_after_commit(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    successor_source = tmp_path / "successor.json"
    successor_source.write_bytes(b"successor")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace
    real_stat = atomic_commit_mod.os.stat
    committed = False
    injected = False

    def track_commit(source, destination, **kwargs):
        nonlocal committed
        result = real_rename_noreplace(source, destination, **kwargs)
        if destination == target.name:
            committed = True
        return result

    def install_successor_before_postcheck(path, *args, **kwargs):
        nonlocal injected
        name = os.fsdecode(path) if isinstance(path, (str, bytes)) else ""
        if committed and not injected and name == target.name and kwargs.get("dir_fd") is not None:
            parent_fd = kwargs["dir_fd"]
            os.rename(
                target.name,
                f"{target.name}.candidate",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.replace(successor_source, target)
            injected = True
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", track_commit)
    monkeypatch.setattr(atomic_commit_mod.os, "stat", install_successor_before_postcheck)

    with pytest.raises(OSError, match="changed during replace") as raised:
        safe_files_mod.write_regular_bytes_atomic(target, b"candidate")

    assert injected is True
    successor = target.stat()
    assert successor.st_nlink == 1
    assert target.read_bytes() == b"successor"
    assert (tmp_path / "owner.json.candidate").read_bytes() == b"candidate"
    assert any(
        "concurrent successor preserved at atomic target" in note
        for note in raised.value.__notes__
    )
    retired_payloads = [
        entry.read_bytes()
        for entry in tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")
    ]
    assert retired_payloads == [b"old"]


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


def test_full_retirement_quota_rejects_before_candidate_creation(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    target.write_bytes(b"old")
    existing = tmp_path / f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}existing"
    existing.write_bytes(b"retired")
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_FILES_PER_DIRECTORY", 1)

    for _attempt in range(3):
        with pytest.raises(OSError, match="retired-file count limit"):
            safe_files_mod.write_regular_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == {target, existing}


def test_atomic_write_rejects_reserved_retirement_destination(tmp_path):
    target = tmp_path / f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}user"

    with pytest.raises(ValueError, match="reserved retirement namespace"):
        safe_files_mod.write_regular_bytes_atomic(target, b"payload")

    assert not target.exists()


def test_declared_byte_reservations_allow_bounded_concurrent_writers(tmp_path):
    barrier = threading.Barrier(2)

    def write_one(name, payload):
        def writer(handle):
            barrier.wait(timeout=5)
            handle.write(payload)

        safe_files_mod.write_regular_file_atomic(
            tmp_path / name,
            writer,
            max_bytes=64 * 1024 * 1024,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write_one, "first.bin", b"first"),
            pool.submit(write_one, "second.bin", b"second"),
        ]
        for future in futures:
            future.result(timeout=10)

    assert (tmp_path / "first.bin").read_bytes() == b"first"
    assert (tmp_path / "second.bin").read_bytes() == b"second"


def test_candidate_reservation_retries_transient_ftruncate_failure(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    real_ftruncate = owned_cleanup_mod.os.ftruncate
    calls = 0

    def fail_first_reservation(fd, length):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient candidate reservation failure")
        return real_ftruncate(fd, length)

    monkeypatch.setattr(owned_cleanup_mod.os, "ftruncate", fail_first_reservation)

    safe_files_mod.write_regular_bytes_atomic(target, b"payload")

    assert calls >= 2
    assert target.read_bytes() == b"payload"
    assert not list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))


def test_failed_candidate_reservation_is_finalized_and_registered(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"

    def fail_reservation(_fd, _length):
        raise OSError("persistent candidate reservation failure")

    monkeypatch.setattr(owned_cleanup_mod.os, "ftruncate", fail_reservation)

    with pytest.raises(OSError, match="persistent candidate reservation failure"):
        safe_files_mod.write_regular_bytes_atomic(target, b"payload")

    assert not target.exists()
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 1
    assert retired[0].stat().st_size == 0
    assert retirement_registry_mod.registered_retirement_paths(tmp_path) == (
        retired[0].name,
    )


def test_bounded_writer_never_extends_candidate_past_declared_limit(tmp_path):
    target = tmp_path / "bounded.bin"

    def oversized(handle):
        handle.write(b"overflow")

    with pytest.raises(OSError, match="exceeds 3-byte retirement budget"):
        safe_files_mod.write_regular_file_atomic(
            target,
            oversized,
            max_bytes=3,
        )

    assert not target.exists()
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 1
    assert retired[0].stat().st_size == 3


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
