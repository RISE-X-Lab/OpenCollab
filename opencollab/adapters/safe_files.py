"""Bounded regular-file operations used by OpenCollab adapters."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, TextIO

from opencollab.adapters._env_base import TextFileRange
from opencollab.adapters.safe_paths import canonicalize_system_path

_LOCK_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 1024 * 1024
_RANGE_READ_CHUNK_CHARS = 64 * 1024
_RANGE_TOTAL_COUNT_LIMIT_BYTES = 256 * 1024
_HAS_DIRFD_OPEN = os.open in os.supports_dir_fd


def _absolute(path: str | os.PathLike[str]) -> Path:
    return canonicalize_system_path(path)


def _directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow or not _HAS_DIRFD_OPEN:
        raise NotImplementedError("race-safe local file access requires POSIX dirfd and O_NOFOLLOW support")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def open_directory_anchor(path: str | os.PathLike[str]) -> int:
    """Open and pin one real directory for later descriptor-relative access."""
    target = _absolute(path)
    fd = os.open(target, _directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise NotADirectoryError(target)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_anchor_path(root_fd: int, root_path: str | os.PathLike[str]) -> None:
    """Keep the old explicit-failure contract if the workspace name is replaced."""
    try:
        current_fd = os.open(_absolute(root_path), _directory_open_flags())
    except OSError as exc:
        raise OSError(f"local workspace is not a real directory: {root_path}") from exc
    try:
        if not _same_file(os.fstat(root_fd), os.fstat(current_fd)):
            raise OSError(f"local workspace was replaced: {root_path}")
    finally:
        os.close(current_fd)


def _relative_components(path: str | os.PathLike[str]) -> tuple[list[str], str]:
    value = os.fspath(path)
    if not value or "\0" in value or os.path.isabs(value):
        raise ValueError("descriptor-relative path must be non-empty relative text")
    normalized = os.path.normpath(value)
    if normalized in {"", ".", ".."} or normalized.startswith(f"..{os.sep}"):
        raise PermissionError(f"path escapes anchored directory: {value}")
    parts = normalized.split(os.sep)
    if any(part in {"", ".", ".."} for part in parts):
        raise PermissionError(f"path escapes anchored directory: {value}")
    return parts[:-1], parts[-1]


def _open_parent_beneath(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    create: bool,
) -> tuple[int, str]:
    """Open a target parent without ever resolving a component by pathname."""
    _require_anchor_path(root_fd, root_path)
    parents, name = _relative_components(path)
    current_fd = os.dup(root_fd)
    try:
        for component in parents:
            try:
                next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, name
    except BaseException:
        os.close(current_fd)
        raise


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


def read_regular_bytes_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
) -> bytes:
    """Read a bounded regular file beneath a pinned directory descriptor."""
    limit = _require_limit(max_bytes)
    parent_fd, name = _open_parent_beneath(root_fd, root_path, path, create=False)
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"read target is not a regular file: {path}")
        if opened.st_size > limit:
            raise ValueError(f"read target exceeds {limit}-byte limit: {path}")
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
            raise ValueError(f"read target exceeds {limit}-byte limit: {path}")
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


def _read_text_line(
    handle: TextIO,
    *,
    collect_limit: int | None,
) -> tuple[str | None, bool, bool]:
    parts: list[str] = []
    seen_content = False
    remaining = collect_limit
    while True:
        chunk = handle.readline(_RANGE_READ_CHUNK_CHARS)
        if chunk == "":
            if not seen_content:
                return None, True, False
            return "".join(parts), True, False
        seen_content = True
        ended = chunk.endswith("\n")
        content = chunk[:-1] if ended else chunk
        if remaining is not None:
            if len(content) > remaining:
                parts.append(content[:remaining])
                return "".join(parts), False, True
            parts.append(content)
            remaining -= len(content)
        if ended:
            return "".join(parts), False, False


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


def read_regular_text_range_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    offset: int,
    limit: int,
    max_chars: int,
    encoding: str = "utf-8",
) -> TextFileRange:
    """Read a logical-line window beneath a pinned directory descriptor."""
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
    parent_fd, name = _open_parent_beneath(root_fd, root_path, path, create=False)
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"read target is not a regular file: {path}")
        count_to_eof = opened.st_size <= _RANGE_TOTAL_COUNT_LIMIT_BYTES
        with os.fdopen(fd, "r", encoding=encoding, errors="strict", newline=None) as handle:
            fd = -1
            line_number = 0
            while line_number < offset - 1:
                line, at_eof, _truncated = _read_text_line(handle, collect_limit=None)
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
                    return TextFileRange(selected, offset, None, True, chars_truncated=True)
                line, at_eof, truncated = _read_text_line(handle, collect_limit=remaining)
                if line is None:
                    return TextFileRange(selected, offset, line_number, False)
                selected.append(line)
                line_number += 1
                chars_used += separator_chars + len(line)
                if truncated:
                    return TextFileRange(selected, offset, None, True, chars_truncated=True)
                if at_eof:
                    return TextFileRange(selected, offset, line_number, False)
            if count_to_eof:
                shown_end = offset - 1 + len(selected)
                while True:
                    line, at_eof, _truncated = _read_text_line(handle, collect_limit=None)
                    if line is None:
                        return TextFileRange(selected, offset, line_number, line_number > shown_end)
                    line_number += 1
                    if at_eof:
                        return TextFileRange(selected, offset, line_number, line_number > shown_end)
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
        os.close(parent_fd)


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


def _current_regular_at(parent_fd: int, name: str, *, context: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{context} target is not a regular file: {name}")
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


def write_regular_file_atomic_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    mode: int = 0o600,
    context: str = "atomic output",
    create_only: bool = False,
) -> None:
    """Atomically publish one file beneath a pinned directory descriptor."""
    limit = _require_limit(max_bytes)
    parent_fd, name = _open_parent_beneath(root_fd, root_path, path, create=True)
    current = _current_regular_at(parent_fd, name, context=context)
    if create_only and current is not None:
        os.close(parent_fd)
        raise FileExistsError(f"{context} target already exists: {path}")
    publish_mode = mode if current is None else stat.S_IMODE(current.st_mode)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            publish_mode,
            dir_fd=parent_fd,
        )
        if current is not None:
            os.fchmod(fd, publish_mode)
        with os.fdopen(fd, "w+b") as handle:
            fd = -1
            writer(handle)
            handle.flush()
            if os.fstat(handle.fileno()).st_size > limit:
                raise ValueError(f"{context} exceeds {limit}-byte limit: {path}")
            os.fsync(handle.fileno())
        if create_only:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_fd)
        else:
            os.rename(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def write_regular_bytes_atomic_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
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
    write_regular_file_atomic_at(
        root_fd,
        root_path,
        path,
        lambda handle: handle.write(payload),
        max_bytes=limit,
        mode=mode,
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


def create_regular_bytes_atomic_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
) -> None:
    limit = len(payload) if max_bytes is None else _require_limit(max_bytes)
    if len(payload) > limit:
        raise ValueError(f"atomic payload exceeds {limit}-byte limit: {path}")
    write_regular_file_atomic_at(
        root_fd,
        root_path,
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


def unlink_regular_file_durable_at(
    root_fd: int,
    root_path: str | os.PathLike[str],
    path: str | os.PathLike[str],
) -> bool:
    parent_fd, name = _open_parent_beneath(root_fd, root_path, path, create=False)
    try:
        current = _current_regular_at(parent_fd, name, context="owned file")
        if current is None:
            return False
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


__all__ = [
    "append_regular_text",
    "create_regular_bytes_atomic",
    "create_regular_bytes_atomic_at",
    "canonicalize_system_path",
    "ensure_directory_no_symlinks",
    "open_regular_text_append",
    "open_directory_anchor",
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
]
