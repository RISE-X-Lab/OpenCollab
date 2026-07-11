"""Bounded input discovery and durable manifest output for smoke batches."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable

from opencollab.adapters.safe_files import (
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    regular_path_identity,
)
from opencollab.harness.swe_eval_records import (
    RecordInputFormatError,
    RecordInputLimitError,
    is_completed_prediction,
    read_bounded_json,
)


def read_instance(path: Path, *, max_bytes: int) -> dict[str, Any]:
    document = read_bounded_json(path, max_bytes=max_bytes)
    if document is None or not isinstance(document[0], dict):
        raise ValueError(f"instance input is not a bounded regular JSON object: {path}")
    return document[0]


def read_prediction_rows(
    path: Path,
    *,
    scan_bytes: int,
    line_bytes: int,
    retained_rows: int,
    retained_bytes: int,
) -> list[dict[str, Any]]:
    try:
        payload = read_regular_bytes(path, max_bytes=scan_bytes)
    except FileNotFoundError:
        return []
    used_bytes = 0
    rows: list[dict[str, Any]] = []
    for raw_line in payload.splitlines(keepends=True):
        if not raw_line.strip():
            continue
        if len(raw_line) > line_bytes:
            raise RecordInputLimitError(f"JSONL line exceeds {line_bytes} bytes: {path}")
        used_bytes += len(raw_line)
        if len(rows) >= retained_rows or used_bytes > retained_bytes:
            raise RecordInputLimitError(f"JSONL input exceeds retained row or byte limit: {path}")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordInputFormatError(f"invalid JSONL record in {path}") from exc
        if not isinstance(value, dict):
            raise RecordInputFormatError(f"JSONL record must be an object: {path}")
        rows.append(value)
    return rows


def prediction_has_patch(
    output: Path,
    instance_id: str,
    *,
    read_rows: Callable[[Path], list[dict[str, Any]]],
) -> bool:
    latest: dict[str, Any] | None = None
    for record in read_rows(output):
        if record.get("instance_id") == instance_id:
            latest = record
    return is_completed_prediction(latest)


def fsync_directory(path: Path) -> None:
    fd = _open_directory_no_symlinks(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def acquire_exclusive_lock(fd: int, *, label: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring {label} after {timeout:g}s")
        time.sleep(min(0.01, remaining))


def open_regular_append(path: Path, *, retries: int) -> int:
    path = Path(os.path.abspath(os.fspath(path)))
    ensure_directory_no_symlinks(path.parent)
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _parent_attempt in range(retries):
        parent_fd = _open_directory_no_symlinks(path.parent)
        try:
            try:
                before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise OSError(f"refusing non-regular smoke manifest: {path}")
            try:
                if before is None:
                    fd = os.open(
                        path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o644,
                        dir_fd=parent_fd,
                    )
                else:
                    fd = os.open(path.name, flags, dir_fd=parent_fd)
            except (FileExistsError, FileNotFoundError):
                continue
            try:
                opened = os.fstat(fd)
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                    raise OSError(f"refusing non-regular smoke manifest: {path}")
                identity = (opened.st_dev, opened.st_ino)
                if (current.st_dev, current.st_ino) != identity:
                    continue
                if before is not None and (before.st_dev, before.st_ino) != identity:
                    continue
                if not _directory_path_matches_fd(path.parent, parent_fd):
                    break
                result_fd = fd
                fd = -1
                return result_fd
            except FileNotFoundError:
                pass
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(parent_fd)
    raise OSError(f"smoke manifest did not stabilize while opening: {path}")


def write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting smoke manifest")
        view = view[written:]


def append_manifest_record(
    path: Path,
    record: dict[str, Any],
    *,
    max_bytes: int,
    retries: int,
    lock_timeout: float,
    sync_directory: Callable[[Path], None],
) -> None:
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = open_regular_append(path, retries=retries)
    locked = False
    try:
        acquire_exclusive_lock(fd, label=f"manifest lock {path}", timeout=lock_timeout)
        locked = True
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > max_bytes:
            raise OSError(f"smoke manifest exceeds byte limit: {path}")
        if needs_separator:
            write_all(fd, b"\n")
        write_all(fd, payload)
        os.fsync(fd)
        opened = os.fstat(fd)
        current = regular_path_identity(path)
        if (opened.st_dev, opened.st_ino, opened.st_size) != current[:3]:
            raise OSError(f"smoke manifest changed while appending: {path}")
        sync_directory(path.parent)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def discover_instance_paths(
    instances_dir: Path,
    *,
    limit: int,
    max_entries: int,
) -> list[Path]:
    try:
        root_info = instances_dir.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"instances directory does not exist: {instances_dir}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"instances path must be a real directory: {instances_dir}")
    paths: list[Path] = []
    scanned_entries = 0
    with os.scandir(instances_dir) as entries:
        for entry in entries:
            scanned_entries += 1
            if scanned_entries > max_entries:
                raise ValueError(f"instances directory exceeds {max_entries} entries")
            if entry.name.endswith(".json"):
                paths.append(Path(entry.path))
    return sorted(paths)[:limit]
