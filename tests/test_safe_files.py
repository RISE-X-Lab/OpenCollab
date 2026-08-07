"""Public behavior tests for the compact safe-file primitives."""

from __future__ import annotations

import concurrent.futures
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import opencollab.adapters.safe_files as safe_files
from opencollab.adapters.safe_files import (
    append_regular_text,
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    read_regular_text_range,
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


def test_ranged_text_read_handles_utf8_crlf_and_missing_final_newline(tmp_path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes("零\r\n一\r\n二".encode())

    window = read_regular_text_range(
        path,
        offset=2,
        limit=2,
        max_chars=20,
    )

    assert window.lines == ["一", "二"]
    assert window.start_line == 2
    assert window.total_lines == 3
    assert not window.has_more
    assert not window.chars_truncated


def test_ranged_text_read_bounds_one_long_line(tmp_path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"x" * (1024 * 1024))

    window = read_regular_text_range(
        path,
        offset=1,
        limit=1,
        max_chars=32,
    )

    assert window.lines == ["x" * 32]
    assert window.total_lines is None
    assert window.has_more
    assert window.chars_truncated


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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases only")
@pytest.mark.parametrize(
    ("alias_root", "canonical_root"),
    [("/tmp", "/private/tmp"), ("/var/tmp", "/private/var/tmp")],
)
def test_macos_system_directory_aliases_match_canonical_paths(
    alias_root,
    canonical_root,
) -> None:
    with tempfile.TemporaryDirectory(dir=canonical_root) as directory:
        canonical_directory = Path(directory)
        alias_directory = Path(alias_root) / canonical_directory.relative_to(canonical_root)
        alias_path = alias_directory / "state"
        canonical_path = canonical_directory / "state"

        write_regular_bytes_atomic(alias_path, b"payload")

        assert read_regular_bytes(alias_path, max_bytes=7) == b"payload"
        assert canonical_path.read_bytes() == b"payload"


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


def test_atomic_replace_fsyncs_parent_after_publish(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def record_fsync(fd: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        original_fsync(fd)

    def record_replace(source, target) -> None:
        events.append("replace")
        original_replace(source, target)

    monkeypatch.setattr(safe_files.os, "fsync", record_fsync)
    monkeypatch.setattr(safe_files.os, "replace", record_replace)

    write_regular_bytes_atomic(tmp_path / "state", b"new")

    assert events[:3] == ["fsync:file", "replace", "fsync:directory"]


def test_atomic_create_fsyncs_parent_after_link_and_unlink(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    original_fsync = os.fsync
    original_link = os.link
    original_unlink = os.unlink

    def record_fsync(fd: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        original_fsync(fd)

    def record_link(source, target, *, follow_symlinks=True) -> None:
        events.append("link")
        original_link(source, target, follow_symlinks=follow_symlinks)

    def record_unlink(path) -> None:
        events.append("unlink")
        original_unlink(path)

    monkeypatch.setattr(safe_files.os, "fsync", record_fsync)
    monkeypatch.setattr(safe_files.os, "link", record_link)
    monkeypatch.setattr(safe_files.os, "unlink", record_unlink)

    create_regular_bytes_atomic(tmp_path / "state", b"new")

    assert events[:4] == ["fsync:file", "link", "unlink", "fsync:directory"]


def test_durable_unlink_fsyncs_parent_after_removal(tmp_path, monkeypatch) -> None:
    path = tmp_path / "owned"
    path.write_bytes(b"owned")
    events: list[str] = []
    original_fsync = os.fsync
    original_unlink = os.unlink

    def record_fsync(fd: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        original_fsync(fd)

    def record_unlink(target) -> None:
        events.append("unlink")
        original_unlink(target)

    monkeypatch.setattr(safe_files.os, "fsync", record_fsync)
    monkeypatch.setattr(safe_files.os, "unlink", record_unlink)

    assert unlink_regular_file_durable(path)
    assert events == ["unlink", "fsync:directory"]


def test_durable_unlink_rejects_symlinked_parent(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = outside / "owned"
    foreign.write_bytes(b"foreign")
    parent = tmp_path / "parent"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="real directory"):
        unlink_regular_file_durable(parent / "owned")

    assert foreign.read_bytes() == b"foreign"
