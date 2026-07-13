"""Public behavior tests for the compact safe-file primitives."""

from __future__ import annotations

import concurrent.futures
import os
import stat

import pytest
from opencollab.adapters.safe_files import (
    append_regular_text,
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
    write_regular_file_atomic,
)


def test_bounded_read_accepts_regular_file_and_rejects_large_file(tmp_path) -> None:
    path = tmp_path / "value.bin"
    path.write_bytes(b"payload")
    assert read_regular_bytes(path, max_bytes=7) == b"payload"
    with pytest.raises(ValueError, match="exceeds"):
        read_regular_bytes(path, max_bytes=6)


def test_missing_read_does_not_create_parent_directories(tmp_path) -> None:
    parent = tmp_path / "missing" / "nested"
    with pytest.raises(FileNotFoundError):
        read_regular_bytes(parent / "value", max_bytes=10)
    assert not (tmp_path / "missing").exists()


def test_read_rejects_final_symlink_and_symlink_parent(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_regular_bytes(link, max_bytes=100)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "parent"
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="real directory"):
        ensure_directory_no_symlinks(parent)


def test_write_rejects_nested_ancestor_symlink(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="real directory"):
        write_regular_bytes_atomic(workspace / "link" / "sub" / "value", b"escape")
    assert not (outside / "sub").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_read_rejects_fifo_without_blocking(tmp_path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(OSError, match="regular file"):
        read_regular_bytes(path, max_bytes=100)


def test_atomic_replace_and_create_only_have_clear_semantics(tmp_path) -> None:
    path = tmp_path / "state"
    path.write_bytes(b"old")
    write_regular_bytes_atomic(path, b"new")
    assert path.read_bytes() == b"new"
    with pytest.raises(FileExistsError):
        create_regular_bytes_atomic(path, b"foreign")
    assert path.read_bytes() == b"new"


def test_atomic_writer_failure_preserves_previous_file(tmp_path) -> None:
    path = tmp_path / "state"
    path.write_bytes(b"old")

    def fail(handle) -> None:
        handle.write(b"partial")
        raise RuntimeError("writer failed")

    with pytest.raises(RuntimeError, match="writer failed"):
        write_regular_file_atomic(path, fail, max_bytes=32)
    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob(".state.*.tmp"))


def test_atomic_writer_enforces_size_before_publish(tmp_path) -> None:
    path = tmp_path / "state"
    with pytest.raises(ValueError, match="exceeds"):
        write_regular_file_atomic(path, lambda handle: handle.write(b"too large"), max_bytes=3)
    assert not path.exists()


def test_atomic_output_uses_requested_mode(tmp_path) -> None:
    path = tmp_path / "private"
    write_regular_bytes_atomic(path, b"payload", mode=0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_replace_preserves_existing_mode(tmp_path) -> None:
    path = tmp_path / "executable"
    path.write_bytes(b"old")
    path.chmod(0o755)
    write_regular_bytes_atomic(path, b"new")
    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_atomic_replace_preserves_mode_ignored_by_umask(tmp_path) -> None:
    path = tmp_path / "shared"
    path.write_bytes(b"old")
    path.chmod(0o666)
    previous = os.umask(0o022)
    try:
        write_regular_bytes_atomic(path, b"new")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o666


def test_concurrent_append_keeps_complete_records(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    records = [f"record-{index:03d}\n" for index in range(64)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda record: append_regular_text(path, record), records))
    assert sorted(path.read_text(encoding="utf-8").splitlines()) == sorted(
        record.rstrip("\n") for record in records
    )


def test_concurrent_directory_creation_is_idempotent(tmp_path) -> None:
    path = tmp_path / "shared" / "nested"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: ensure_directory_no_symlinks(path), range(32)))
    assert path.is_dir()


def test_durable_unlink_removes_regular_file_and_reports_missing(tmp_path) -> None:
    path = tmp_path / "owned"
    path.write_bytes(b"owned")
    assert unlink_regular_file_durable(path)
    assert not path.exists()
    assert unlink_regular_file_durable(path) is False
