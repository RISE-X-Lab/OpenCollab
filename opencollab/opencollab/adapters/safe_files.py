"""Small fd-level helpers for append-only local observability files."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
import uuid
from pathlib import Path
from typing import TextIO

_LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.1
_LOCK_RETRY_INTERVAL_SECONDS = 0.001


def _open_directory_no_symlinks(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(absolute.anchor or os.sep, directory_flags)
    try:
        for component in absolute.parts[1:]:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise OSError(f"append parent is not a real directory: {absolute}")
            next_fd = os.open(component, directory_flags, dir_fd=fd)
            opened = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                os.close(next_fd)
                raise OSError(f"append parent changed while opening: {absolute}")
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_directory_no_symlinks(path: str | os.PathLike[str]) -> None:
    """Create a directory tree one component at a time without following links."""
    absolute = Path(os.path.abspath(path))
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(absolute.anchor or os.sep, directory_flags)
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise OSError(f"directory parent is not a real directory: {absolute}")
            next_fd = os.open(component, directory_flags, dir_fd=fd)
            opened = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                os.close(next_fd)
                raise OSError(f"directory parent changed while opening: {absolute}")
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def _directory_path_matches_fd(path: Path, fd: int) -> bool:
    verified_fd = _open_directory_no_symlinks(path)
    try:
        original = os.fstat(fd)
        verified = os.fstat(verified_fd)
        return (original.st_dev, original.st_ino) == (
            verified.st_dev,
            verified.st_ino,
        )
    finally:
        os.close(verified_fd)


def open_regular_text_append(path: str | os.PathLike[str]) -> TextIO:
    """Open or exclusively create a final-component regular file for append."""
    target = Path(os.path.abspath(path))
    base_flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _parent_attempt in range(3):
        parent_fd = _open_directory_no_symlinks(target.parent)
        try:
            for _target_attempt in range(3):
                before: os.stat_result | None
                try:
                    before = os.stat(
                        target.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    before = None
                if before is not None and not stat.S_ISREG(before.st_mode):
                    raise OSError(f"append target is not a regular file: {target}")

                flags = base_flags
                if before is None:
                    flags |= os.O_CREAT | os.O_EXCL
                fd: int | None = None
                try:
                    fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                except FileNotFoundError:
                    continue
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode):
                        raise OSError(f"append target is not a regular file: {target}")
                    if before is not None and (
                        before.st_dev,
                        before.st_ino,
                    ) != (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        continue
                    try:
                        current = os.stat(
                            target.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(current.st_mode):
                        raise OSError(f"append target is not a regular file: {target}")
                    if (current.st_dev, current.st_ino) != (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        continue
                    if not _directory_path_matches_fd(target.parent, parent_fd):
                        break
                    handle = os.fdopen(fd, "a", encoding="utf-8")
                    fd = None
                    return handle
                finally:
                    if fd is not None:
                        os.close(fd)
        finally:
            os.close(parent_fd)
    raise OSError(f"append target changed repeatedly while opening: {target}")


def read_regular_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bytes:
    """Read one stable regular file with a strict byte limit."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    target = Path(os.path.abspath(path))
    parent_fd = _open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"read target parent identity changed: {target.parent}")
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
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"read target changed while opening: {target}")
        if opened.st_size > max_bytes:
            raise ValueError(f"read target exceeds {max_bytes}-byte limit: {target}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ValueError(f"read target exceeds {max_bytes}-byte limit: {target}")
        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(opened) != identity(after):
            raise OSError(f"read target changed while reading: {target}")
        try:
            current = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise OSError(f"read target changed while reading: {target}") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            raise OSError(f"read target changed while reading: {target}")
        if not _directory_path_matches_fd(target.parent, parent_fd):
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
    parent_fd = _open_directory_no_symlinks(target.parent)
    try:
        try:
            current = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            return False
        return _directory_path_matches_fd(target.parent, parent_fd)
    finally:
        os.close(parent_fd)


def regular_path_identity(
    path: str | os.PathLike[str],
) -> tuple[int, int, int, int, int]:
    """Return stable identity and mutation metadata for one regular path."""
    target = Path(os.path.abspath(path))
    parent_fd = _open_directory_no_symlinks(target.parent)
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
            or (before.st_dev, before.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"identity target changed while opening: {target}")
        current = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"identity target changed while opening: {target}")
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"identity target parent changed: {target.parent}")
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def write_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Durably replace a regular file through a verified parent dirfd."""
    if not isinstance(payload, bytes):
        raise TypeError("atomic payload must be bytes")
    if max_bytes is not None and (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        raise ValueError("max_bytes must be a non-negative integer or None")
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError(f"atomic payload exceeds {max_bytes}-byte limit: {path}")
    target = Path(os.path.abspath(path))
    if not target.name or target.name in {".", ".."}:
        raise ValueError(f"invalid atomic output path: {path}")
    ensure_directory_no_symlinks(target.parent)
    parent_fd = _open_directory_no_symlinks(target.parent)
    # Keep the temporary component independent of the destination basename.
    # A legal 255-byte target name must not overflow NAME_MAX after suffixing.
    temporary = f".oc-{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    replaced = False
    written_identity: tuple[int, int] | None = None
    primary_error: BaseException | None = None
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"atomic output parent identity changed: {target.parent}")
        fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"atomic write made no progress: {target}")
            view = view[written:]
        os.fsync(fd)
        written_info = os.fstat(fd)
        written_identity = (written_info.st_dev, written_info.st_ino)
        os.close(fd)
        fd = -1
        try:
            existing = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"atomic output target is not a regular file: {target}")
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"atomic output parent changed before replace: {target.parent}")
        os.replace(
            temporary,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        replaced = True
        current = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != written_identity
        ):
            raise OSError(f"atomic output changed during replace: {target}")
        os.fsync(parent_fd)
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"atomic output parent changed after replace: {target.parent}")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        if fd >= 0:
            try:
                os.close(fd)
            except BaseException as exc:
                cleanup_errors.append(("temporary fd close", exc))
        if not replaced:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                cleanup_errors.append((f"temporary unlink {temporary}", exc))
        try:
            os.close(parent_fd)
        except BaseException as exc:
            cleanup_errors.append(("parent directory fd close", exc))
        if cleanup_errors:
            if primary_error is not None:
                for stage, cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"atomic cleanup {stage} failed with "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            else:
                stage, cleanup_error = cleanup_errors[0]
                for extra_stage, extra_error in cleanup_errors[1:]:
                    cleanup_error.add_note(
                        f"additional atomic cleanup {extra_stage} failed with "
                        f"{type(extra_error).__name__}: {extra_error}"
                    )
                cleanup_error.add_note(f"atomic cleanup stage: {stage}")
                raise cleanup_error


def unlink_regular_file_durable(
    path: str | os.PathLike[str],
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bool:
    """Unlink one regular file through a stable parent and fsync the directory."""
    target = Path(os.path.abspath(path))
    parent_fd = _open_directory_no_symlinks(target.parent)
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"unlink target parent identity changed: {target.parent}")
        try:
            current = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(current.st_mode):
            raise OSError(f"unlink target is not a regular file: {target}")
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"unlink target parent changed: {target.parent}")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"unlink target parent changed: {target.parent}")
        return True
    finally:
        os.close(parent_fd)


def append_regular_text(path: str | os.PathLike[str], text: str) -> None:
    with open_regular_text_append(path) as handle:
        write_locked_text(handle, text)
        if not regular_handle_matches_path(handle, path):
            raise OSError(f"append target changed while writing: {path}")


def _acquire_append_lock(fd: int) -> None:
    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_LOCK_RETRY_INTERVAL_SECONDS, remaining))


def write_locked_text(handle: TextIO, text: str) -> None:
    """Write one UTF-8 record under a fail-fast advisory append lock.

    Observability writes must never wait indefinitely behind another process.
    Callers deliberately treat a busy lock like any other write failure: the
    tracer records a sticky error and the event sink drops that event.
    """
    payload = text.encode("utf-8")
    fd = handle.fileno()
    _acquire_append_lock(fd)
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
    "append_regular_text",
    "ensure_directory_no_symlinks",
    "open_regular_text_append",
    "read_regular_bytes",
    "read_regular_text",
    "regular_handle_matches_path",
    "regular_path_identity",
    "unlink_regular_file_durable",
    "write_regular_bytes_atomic",
    "write_locked_text",
]
