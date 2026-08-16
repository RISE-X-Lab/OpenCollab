"""Bounded regular-file operations used by OpenCollab adapters."""

from __future__ import annotations

import errno
import fcntl
import math
import os
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, TextIO

from opencollab.adapters._env_base import TextFileRange
from opencollab.adapters._safe_file_common import (
    _RANGE_TOTAL_COUNT_LIMIT_BYTES,
    _READ_CHUNK_BYTES,
    _read_text_line,
    _require_limit,
)
from opencollab.adapters.safe_anchored_files import (
    create_regular_bytes_atomic_at,
    open_directory_anchor,
    read_regular_bytes_at,
    read_regular_text_range_at,
    unlink_regular_file_durable_at,
    write_regular_bytes_atomic_at,
    write_regular_file_atomic_at,
)
from opencollab.adapters.safe_paths import canonicalize_system_path

_LOCK_TIMEOUT_SECONDS = 10.0


def _absolute(path: str | os.PathLike[str]) -> Path:
    return canonicalize_system_path(path)


def _check_directory_components(path: str | os.PathLike[str], *, create: bool) -> None:
    target = _absolute(path)
    current = Path(target.anchor or os.sep)
    for component in target.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                info = os.lstat(current)
            else:
                continue
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"directory is not a real directory: {current}")


def ensure_directory_no_symlinks(path: str | os.PathLike[str]) -> None:
    """Create a directory after one static check of every existing component."""
    _check_directory_components(path, create=True)


def read_regular_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
) -> bytes:
    """Read one bounded regular file without following its final symlink."""
    target = _absolute(path)
    limit = _require_limit(max_bytes)
    _check_directory_components(target.parent, create=False)
    fd = os.open(
        target,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"read target is not a regular file: {target}")
        if opened.st_size > limit:
            raise ValueError(f"read target exceeds {limit}-byte limit: {target}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise ValueError(f"read target exceeds {limit}-byte limit: {target}")
        return payload
    finally:
        os.close(fd)


def read_regular_text(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    return read_regular_bytes(path, max_bytes=max_bytes).decode(encoding)


def read_regular_text_range(
    path: str | os.PathLike[str],
    *,
    offset: int,
    limit: int,
    max_chars: int,
    encoding: str = "utf-8",
) -> TextFileRange:
    """Read a bounded logical-line window without loading the full file."""
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 1
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 1
    ):
        raise ValueError("offset, limit, and max_chars must be positive integers")
    target = _absolute(path)
    _check_directory_components(target.parent, create=False)
    fd = os.open(
        target,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"read target is not a regular file: {target}")
        count_to_eof = opened.st_size <= _RANGE_TOTAL_COUNT_LIMIT_BYTES
        with os.fdopen(fd, "r", encoding=encoding, errors="strict", newline=None) as handle:
            fd = -1
            line_number = 0
            while line_number < offset - 1:
                line, at_eof, _truncated = _read_text_line(
                    handle,
                    collect_limit=None,
                )
                if line is None:
                    return TextFileRange([], offset, line_number, False)
                line_number += 1
                if at_eof:
                    return TextFileRange([], offset, line_number, False)

            selected: list[str] = []
            chars_used = 0
            while len(selected) < limit:
                separator_chars = 1 if selected else 0
                remaining = max_chars - chars_used - separator_chars
                if remaining < 1:
                    return TextFileRange(
                        selected,
                        offset,
                        None,
                        True,
                        chars_truncated=True,
                    )
                line, at_eof, truncated = _read_text_line(
                    handle,
                    collect_limit=remaining,
                )
                if line is None:
                    return TextFileRange(
                        selected,
                        offset,
                        line_number,
                        False,
                    )
                selected.append(line)
                line_number += 1
                chars_used += separator_chars + len(line)
                if truncated:
                    return TextFileRange(
                        selected,
                        offset,
                        None,
                        True,
                        chars_truncated=True,
                    )
                if at_eof:
                    return TextFileRange(
                        selected,
                        offset,
                        line_number,
                        False,
                    )

            if count_to_eof:
                shown_end = offset - 1 + len(selected)
                while True:
                    line, at_eof, _truncated = _read_text_line(
                        handle,
                        collect_limit=None,
                    )
                    if line is None:
                        return TextFileRange(
                            selected,
                            offset,
                            line_number,
                            line_number > shown_end,
                        )
                    line_number += 1
                    if at_eof:
                        return TextFileRange(
                            selected,
                            offset,
                            line_number,
                            line_number > shown_end,
                        )
            has_more = handle.read(1) != ""
            return TextFileRange(
                selected,
                offset,
                None if has_more else line_number,
                has_more,
            )
    finally:
        if fd >= 0:
            os.close(fd)


def open_regular_text_append(
    path: str | os.PathLike[str],
    *,
    readable: bool = False,
) -> TextIO:
    target = _absolute(path)
    ensure_directory_no_symlinks(target.parent)
    _current_regular(target, context="append")
    fd = os.open(
        target,
        (os.O_RDWR if readable else os.O_WRONLY)
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"append target is not a regular file: {target}")
    return os.fdopen(fd, "a", encoding="utf-8")


def acquire_exclusive_lock(
    fd: int,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    context: str = "file lock",
) -> None:
    """Acquire an advisory lock with a bounded non-blocking retry loop."""
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
    ):
        raise ValueError("lock timeout must be finite and non-negative")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring {context}") from exc
            time.sleep(0.01)


def _acquire_append_lock(fd: int) -> None:
    acquire_exclusive_lock(fd, context="append lock")


def write_locked_text(handle: TextIO, text: str) -> None:
    fd = handle.fileno()
    _acquire_append_lock(fd)
    try:
        payload = memoryview(text.encode("utf-8"))
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError("append write made no progress")
            payload = payload[written:]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def append_regular_text(path: str | os.PathLike[str], text: str) -> None:
    with open_regular_text_append(path) as handle:
        write_locked_text(handle, text)


def _current_regular(path: Path, *, context: str) -> os.stat_result | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{context} target is not a regular file: {path}")
    return info


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"fsync target is not a directory: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def write_regular_file_atomic(
    path: str | os.PathLike[str],
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    mode: int = 0o600,
    context: str = "atomic output",
    create_only: bool = False,
) -> None:
    """Write a bounded temporary and publish it with one atomic rename."""
    target = _absolute(path)
    limit = _require_limit(max_bytes)
    ensure_directory_no_symlinks(target.parent)
    current = _current_regular(target, context=context)
    if create_only and current is not None:
        raise FileExistsError(f"{context} target already exists: {target}")
    publish_mode = mode if current is None else stat.S_IMODE(current.st_mode)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        publish_mode,
    )
    try:
        if current is not None:
            os.fchmod(fd, publish_mode)
        with os.fdopen(fd, "w+b") as handle:
            fd = -1
            writer(handle)
            handle.flush()
            if os.fstat(handle.fileno()).st_size > limit:
                raise ValueError(f"{context} exceeds {limit}-byte limit: {target}")
            os.fsync(handle.fileno())
        if create_only:
            os.link(temporary, target, follow_symlinks=False)
            os.unlink(temporary)
        else:
            os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("atomic payload must be bytes")
    limit = len(payload) if max_bytes is None else _require_limit(max_bytes)
    if len(payload) > limit:
        raise ValueError(f"atomic payload exceeds {limit}-byte limit: {path}")
    write_regular_file_atomic(
        path,
        lambda handle: handle.write(payload),
        max_bytes=limit,
        mode=mode,
        create_only=False,
    )


def create_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
) -> None:
    limit = len(payload) if max_bytes is None else _require_limit(max_bytes)
    if len(payload) > limit:
        raise ValueError(f"atomic payload exceeds {limit}-byte limit: {path}")
    write_regular_file_atomic(
        path,
        lambda handle: handle.write(payload),
        max_bytes=limit,
        mode=mode,
        create_only=True,
    )


def unlink_regular_file_durable(path: str | os.PathLike[str]) -> bool:
    target = _absolute(path)
    _check_directory_components(target.parent, create=False)
    current = _current_regular(target, context="owned file")
    if current is None:
        return False
    os.unlink(target)
    _fsync_directory(target.parent)
    return True


__all__ = [
    "acquire_exclusive_lock",
    "append_regular_text",
    "create_regular_bytes_atomic",
    "create_regular_bytes_atomic_at",
    "canonicalize_system_path",
    "ensure_directory_no_symlinks",
    "open_directory_anchor",
    "open_regular_text_append",
    "read_regular_bytes",
    "read_regular_bytes_at",
    "read_regular_text",
    "read_regular_text_range",
    "read_regular_text_range_at",
    "unlink_regular_file_durable",
    "unlink_regular_file_durable_at",
    "write_locked_text",
    "write_regular_bytes_atomic",
    "write_regular_bytes_atomic_at",
    "write_regular_file_atomic",
    "write_regular_file_atomic_at",
]
