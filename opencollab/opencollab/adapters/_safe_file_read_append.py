"""Descriptor-safe regular-file reads and append-only writes."""

from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from types import ModuleType
from typing import TextIO

from opencollab.adapters import _safe_file_support as support
from opencollab.adapters._posix_file_support import require_posix_file_support

_LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.1
_LOCK_RETRY_INTERVAL_SECONDS = 0.001
_READ_CHUNK_BYTES = 65_536


def _stat_append_target(target: Path, parent_fd: int) -> os.stat_result | None:
    try:
        before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"append target is not a regular file: {target}")
    return before


def _open_append_fd(target: Path, parent_fd: int, before: os.stat_result | None) -> int:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        return os.open(target.name, flags, 0o600, dir_fd=parent_fd)
    except (FileExistsError, FileNotFoundError):
        return -1


def _append_fd_matches_path(
    target: Path,
    parent_fd: int,
    fd: int,
    before: os.stat_result | None,
) -> bool:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(f"append target is not a regular file: {target}")
    if before is not None and (before.st_dev, before.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        return False
    try:
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(current.st_mode):
        raise OSError(f"append target is not a regular file: {target}")
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _open_append_under_parent(target: Path, parent_fd: int) -> TextIO | None:
    for _target_attempt in range(3):
        before = _stat_append_target(target, parent_fd)
        fd = _open_append_fd(target, parent_fd, before)
        if fd < 0:
            continue
        try:
            if not _append_fd_matches_path(target, parent_fd, fd, before):
                continue
            if not support.directory_path_matches_fd(target.parent, parent_fd):
                return None
            handle = os.fdopen(fd, "a", encoding="utf-8")
            fd = -1
            return handle
        finally:
            if fd >= 0:
                os.close(fd)
    return None


def open_regular_text_append(path: str | os.PathLike[str]) -> TextIO:
    """Open or exclusively create a final-component regular file for append."""
    target = Path(os.path.abspath(path))
    for _parent_attempt in range(3):
        parent_fd = support.open_directory_no_symlinks(target.parent)
        try:
            handle = _open_append_under_parent(target, parent_fd)
            if handle is not None:
                return handle
        finally:
            os.close(parent_fd)
    raise OSError(f"append target changed repeatedly while opening: {target}")


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _mutation_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_regular_read_target(
    target: Path,
    parent_fd: int,
) -> tuple[int, os.stat_result]:
    before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"read target is not a regular file: {target}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(target.name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"read target changed while opening: {target}")
    except BaseException:
        os.close(fd)
        raise
    return fd, opened


def _read_bounded_payload(
    fd: int,
    opened: os.stat_result,
    *,
    max_bytes: int,
    target: Path,
) -> tuple[bytes, os.stat_result]:
    if opened.st_size > max_bytes:
        raise ValueError(f"read target exceeds {max_bytes}-byte limit: {target}")
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise ValueError(f"read target exceeds {max_bytes}-byte limit: {target}")
    if _mutation_identity(opened) != _mutation_identity(after):
        raise OSError(f"read target changed while reading: {target}")
    return payload, after


def _require_visible_read_identity(
    target: Path,
    parent_fd: int,
    after: os.stat_result,
) -> None:
    try:
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise OSError(f"read target changed while reading: {target}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise OSError(f"read target changed while reading: {target}")


def read_regular_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bytes:
    """Read one stable regular file with a strict byte limit."""
    _validate_max_bytes(max_bytes)
    target = Path(os.path.abspath(path))
    parent_fd = support.open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"read target parent identity changed: {target.parent}")
        fd, opened = _open_regular_read_target(target, parent_fd)
        payload, after = _read_bounded_payload(
            fd,
            opened,
            max_bytes=max_bytes,
            target=target,
        )
        _require_visible_read_identity(target, parent_fd, after)
        if not support.directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"read target parent changed while reading: {target}")
        return payload
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def read_regular_text(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    return read_regular_bytes(path, max_bytes=max_bytes).decode(encoding)


def regular_handle_matches_path(
    handle: TextIO,
    path: str | os.PathLike[str],
) -> bool:
    """Return whether an open regular-file handle is still the named path."""
    target = Path(os.path.abspath(path))
    opened = os.fstat(handle.fileno())
    if not stat.S_ISREG(opened.st_mode):
        return False
    parent_fd = support.open_directory_no_symlinks(target.parent)
    try:
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return False
        return support.directory_path_matches_fd(target.parent, parent_fd)
    finally:
        os.close(parent_fd)


def regular_path_identity(
    path: str | os.PathLike[str],
) -> tuple[int, int, int, int, int]:
    """Return stable identity and mutation metadata for one regular path."""
    target = Path(os.path.abspath(path))
    parent_fd = support.open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"identity target is not a regular file: {target}")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(target.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"identity target changed while opening: {target}")
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"identity target changed while opening: {target}")
        if not support.directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"identity target parent changed: {target.parent}")
        return _mutation_identity(opened)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _acquire_append_lock(fd: int) -> ModuleType:
    fcntl = require_posix_file_support()
    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fcntl
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_LOCK_RETRY_INTERVAL_SECONDS, remaining))


def write_locked_text(handle: TextIO, text: str) -> None:
    """Write one UTF-8 record under a fail-fast advisory append lock."""
    payload = text.encode("utf-8")
    fd = handle.fileno()
    fcntl = _acquire_append_lock(fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("append write made no progress")
            view = view[written:]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "open_regular_text_append",
    "read_regular_bytes",
    "read_regular_text",
    "regular_handle_matches_path",
    "regular_path_identity",
    "write_locked_text",
]
