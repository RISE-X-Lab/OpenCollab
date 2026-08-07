"""Bounded regular-file operations used by OpenCollab adapters."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, TextIO

_LOCK_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 1024 * 1024
_MACOS_SYSTEM_ALIASES = (
    (Path("/tmp"), Path("/private/tmp")),
    (Path("/var"), Path("/private/var")),
)


def _canonicalize_system_alias(path: Path) -> Path:
    if sys.platform != "darwin":
        return path
    for alias, canonical in _MACOS_SYSTEM_ALIASES:
        try:
            relative = path.relative_to(alias)
        except ValueError:
            continue
        if Path(os.path.realpath(alias)) == canonical and canonical.is_dir():
            return canonical / relative
    return path


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = os.fspath(path)
    if not value or "\0" in value:
        raise ValueError("path must be non-empty text without NUL bytes")
    return _canonicalize_system_alias(Path(os.path.abspath(value)))


def _require_limit(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    return max_bytes


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
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
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


def open_regular_text_append(path: str | os.PathLike[str]) -> TextIO:
    target = _absolute(path)
    ensure_directory_no_symlinks(target.parent)
    _current_regular(target, context="append")
    fd = os.open(
        target,
        os.O_WRONLY
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


def _acquire_append_lock(fd: int) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out acquiring append lock") from exc
            time.sleep(0.01)


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
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
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
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
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
    "append_regular_text",
    "create_regular_bytes_atomic",
    "ensure_directory_no_symlinks",
    "open_regular_text_append",
    "read_regular_bytes",
    "read_regular_text",
    "unlink_regular_file_durable",
    "write_locked_text",
    "write_regular_bytes_atomic",
    "write_regular_file_atomic",
]
