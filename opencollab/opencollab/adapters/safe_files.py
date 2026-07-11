"""Small fd-level helpers for append-only local observability files."""

from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from types import ModuleType
from typing import BinaryIO, Callable, TextIO

from opencollab.adapters._atomic_file_commit import commit_owned_temporary
from opencollab.adapters._bounded_candidate_writer import write_bounded_candidate
from opencollab.adapters._owned_file_cleanup import (
    RETIRED_FILE_PREFIX,
    create_owned_retirement_candidate,
    quarantine_unlink_owned_file,
)
from opencollab.adapters._posix_file_support import (
    normalize_trusted_root_alias,
    require_posix_file_support,
)
from opencollab.application.exception_notes import add_exception_note

_LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.1
_LOCK_RETRY_INTERVAL_SECONDS = 0.001


def _open_directory_no_symlinks(path: Path) -> int:
    require_posix_file_support()
    absolute = normalize_trusted_root_alias(Path(os.path.abspath(path)))
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
    require_posix_file_support()
    absolute = normalize_trusted_root_alias(Path(os.path.abspath(path)))
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


def _verify_atomic_parent_binding(
    target: Path,
    parent_fd: int,
    *,
    context: str,
    phase: str,
    operation: str,
    expected_target_identity: tuple[int, int] | None = None,
) -> None:
    verified_fd = _open_directory_no_symlinks(target.parent)
    verification_error: BaseException | None = None
    try:
        original = os.fstat(parent_fd)
        verified = os.fstat(verified_fd)
        if (original.st_dev, original.st_ino) != (
            verified.st_dev,
            verified.st_ino,
        ):
            raise OSError(
                f"{context} parent changed {phase} atomic {operation}: {target.parent}"
            )
        if expected_target_identity is not None:
            visible = os.stat(
                target.name,
                dir_fd=verified_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(visible.st_mode)
                or (visible.st_dev, visible.st_ino) != expected_target_identity
            ):
                raise OSError(
                    f"{context} path changed after atomic {operation}: {target}"
                )
    except BaseException as exc:
        verification_error = exc
    try:
        os.close(verified_fd)
    except BaseException as close_error:
        if verification_error is not None:
            add_exception_note(
                verification_error,
                f"{context} verified parent fd close failed with "
                f"{type(close_error).__name__}: {close_error}",
            )
        else:
            raise
    if verification_error is not None:
        raise verification_error


def write_regular_file_atomic(
    path: str | os.PathLike[str],
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
    context: str = "atomic output",
    create_only: bool = False,
) -> None:
    """Write and durably replace one regular file while retaining temp ownership."""
    target = Path(os.path.abspath(path))
    if not target.name or target.name in {".", ".."}:
        raise ValueError(f"invalid {context} path: {path}")
    if target.name.startswith(RETIRED_FILE_PREFIX):
        raise ValueError(f"{context} path uses the reserved retirement namespace: {path}")
    if expected_target_identity is not None and require_target_absent:
        raise ValueError("expected_target_identity and require_target_absent are mutually exclusive")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    ensure_directory_no_symlinks(target.parent)
    parent_fd = _open_directory_no_symlinks(target.parent)
    temporary = ""
    fd = -1
    temporary_identity: tuple[int, int] | None = None
    primary_error: BaseException | None = None
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"{context} parent identity changed: {target.parent}")
        try:
            initial_target = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            initial_target = None
        if initial_target is not None and not stat.S_ISREG(initial_target.st_mode):
            raise OSError(f"{context} target is not a regular file: {target}")
        initial_identity = (
            (initial_target.st_dev, initial_target.st_ino)
            if initial_target is not None
            else None
        )
        if expected_target_identity is not None and initial_identity != expected_target_identity:
            raise OSError(f"atomic target identity changed before commit: {target}")
        if (create_only or require_target_absent) and initial_target is not None:
            raise FileExistsError(f"atomic target appeared before commit: {target}")
        effective_create = initial_target is None
        temporary, fd, temporary_identity, candidate_budget = (
            create_owned_retirement_candidate(
                parent_fd,
                mode=mode,
                required_files=2 if initial_target is not None else 1,
                required_bytes=(
                    max(initial_target.st_size, 0)
                    if initial_target is not None
                    else 0
                ),
                candidate_bytes=max_bytes,
                path_label=str(target),
            )
        )
        write_bounded_candidate(
            fd,
            writer,
            max_bytes=candidate_budget,
            context=context,
        )
        os.fsync(fd)
        written = os.fstat(fd)
        if (
            not stat.S_ISREG(written.st_mode)
            or (written.st_dev, written.st_ino) != temporary_identity
        ):
            raise OSError(f"{context} temporary identity changed: {target}")
        current_temporary = os.stat(
            temporary,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_temporary.st_mode)
            or (current_temporary.st_dev, current_temporary.st_ino)
            != temporary_identity
        ):
            operation = "create" if effective_create else "replace"
            raise OSError(f"{context} temporary path changed before {operation}: {target}")
        _verify_atomic_parent_binding(
            target,
            parent_fd,
            context=context,
            phase="before",
            operation="create" if effective_create else "replace",
        )
        commit_owned_temporary(
            parent_fd,
            temporary,
            target.name,
            fd,
            temporary_identity,
            path_label=str(target),
            expected_target_identity=initial_identity,
            require_target_absent=effective_create,
            create_only=create_only,
            post_commit_check=lambda: _verify_atomic_parent_binding(
                target,
                parent_fd,
                context=context,
                phase="after",
                operation="create" if effective_create else "replace",
                expected_target_identity=temporary_identity,
            ),
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            if not temporary:
                pass
            elif fd < 0 or temporary_identity is None:
                try:
                    os.stat(
                        temporary,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise OSError(
                        f"refusing to retire {context} temporary path without ownership proof"
                    )
            else:
                quarantine_unlink_owned_file(
                    parent_fd,
                    temporary,
                    fd,
                    temporary_identity,
                    path_label=str(target),
                )
        except FileNotFoundError:
            pass
        except BaseException as exc:
            cleanup_errors.append((f"temporary retirement {temporary}", exc))
        if fd >= 0:
            try:
                os.close(fd)
            except BaseException as exc:
                cleanup_errors.append(("temporary fd close", exc))
        try:
            os.close(parent_fd)
        except BaseException as exc:
            cleanup_errors.append(("parent directory fd close", exc))
        if cleanup_errors:
            if primary_error is not None:
                for stage, cleanup_error in cleanup_errors:
                    add_exception_note(
                        primary_error,
                        f"atomic cleanup {stage} failed with "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            else:
                stage, cleanup_error = cleanup_errors[0]
                for extra_stage, extra_error in cleanup_errors[1:]:
                    add_exception_note(
                        cleanup_error,
                        f"additional atomic cleanup {extra_stage} failed with "
                        f"{type(extra_error).__name__}: {extra_error}"
                    )
                add_exception_note(cleanup_error, f"atomic cleanup stage: {stage}")
                raise cleanup_error


def _write_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
    create_only: bool,
) -> None:
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

    def write_payload(handle: BinaryIO) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(handle.fileno(), view)
            if written <= 0:
                raise OSError(f"atomic write made no progress: {path}")
            view = view[written:]

    write_regular_file_atomic(
        path,
        write_payload,
        max_bytes=max_bytes if max_bytes is not None else len(payload),
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        create_only=create_only,
    )


def write_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
) -> None:
    """Durably replace a regular file through a verified parent dirfd."""
    _write_regular_bytes_atomic(
        path,
        payload,
        max_bytes=max_bytes,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        create_only=False,
    )


def create_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Durably create a regular file while refusing an existing destination."""
    _write_regular_bytes_atomic(
        path,
        payload,
        max_bytes=max_bytes,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        require_target_absent=True,
        create_only=True,
    )


def unlink_regular_file_durable(
    path: str | os.PathLike[str],
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
) -> bool:
    """Retire one descriptor-pinned regular file under a unique name."""
    target = Path(os.path.abspath(path))
    parent_fd = _open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        if expected_parent_identity is not None:
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
                raise OSError(f"retirement target parent identity changed: {target.parent}")
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"retirement target is not a regular file: {target}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if not stat.S_ISREG(opened.st_mode) or identity != (before.st_dev, before.st_ino):
            raise OSError(f"retirement target changed while opening: {target}")
        if expected_target_identity is not None and identity != expected_target_identity:
            raise OSError(f"retirement target identity changed: {target}")
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"retirement target parent changed: {target.parent}")
        retired = quarantine_unlink_owned_file(
            parent_fd,
            target.name,
            fd,
            identity,
            path_label=str(target),
        )
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"retirement target parent changed: {target.parent}")
        return retired
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def append_regular_text(path: str | os.PathLike[str], text: str) -> None:
    with open_regular_text_append(path) as handle:
        write_locked_text(handle, text)
        if not regular_handle_matches_path(handle, path):
            raise OSError(f"append target changed while writing: {path}")


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
    """Write one UTF-8 record under a fail-fast advisory append lock.

    Observability writes must never wait indefinitely behind another process.
    Callers deliberately treat a busy lock like any other write failure: the
    tracer records a sticky error and the event sink drops that event.
    """
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
    "append_regular_text",
    "create_regular_bytes_atomic",
    "ensure_directory_no_symlinks",
    "open_regular_text_append",
    "read_regular_bytes",
    "read_regular_text",
    "regular_handle_matches_path",
    "regular_path_identity",
    "unlink_regular_file_durable",
    "write_regular_file_atomic",
    "write_regular_bytes_atomic",
    "write_locked_text",
]
