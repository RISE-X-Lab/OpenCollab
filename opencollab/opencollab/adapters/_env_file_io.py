"""Race-resistant local regular-file operations."""

from __future__ import annotations

import asyncio
import math
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opencollab.adapters._atomic_file_commit import commit_owned_temporary
from opencollab.adapters._owned_file_cleanup import (
    RETIRED_FILE_PREFIX,
    OwnedFileMismatchError,
    create_owned_retirement_candidate,
    quarantine_unlink_owned_file,
)
from opencollab.adapters._posix_file_support import normalize_trusted_root_alias
from opencollab.application.exception_notes import add_exception_note


@dataclass(eq=False)
class _TemporaryFileOwnership:
    """An open descriptor that pins a temporary file's inode until removal."""

    fd: int
    dev: int
    ino: int
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    closed: bool = field(default=False, init=False)


class _TemporaryFileReplacedError(OSError):
    """The owned temporary path now names an object owned by another actor."""


def _positive_finite_timeout(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number of seconds")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
    return timeout


def _positive_file_size_limit(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer number of bytes")
    return value


def _open_regular_file_flags(access: int) -> int:
    return access | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_parent_dirfd(
    path: str,
    *,
    create_parents: bool,
    root_fd: int | None = None,
) -> tuple[int, str]:
    """Resolve every ancestor without following symbolic links."""
    if not isinstance(path, str) or "\0" in path:
        raise ValueError("local file path must be text without NUL bytes")
    if root_fd is None and not os.path.isabs(path):
        raise ValueError("local file path must be absolute without a root descriptor")
    if root_fd is not None and os.path.isabs(path):
        raise ValueError("descriptor-relative local file path must be relative")
    normalized = os.path.normpath(path)
    if root_fd is None:
        normalized = str(normalize_trusted_root_alias(Path(normalized)))
    components = normalized.split(os.sep)[1:] if root_fd is None else normalized.split(os.sep)
    if not components or not components[-1] or components[-1] in {".", ".."}:
        raise OSError(f"local file path has no file component: {path}")
    parent_components = components[:-1]
    current_fd = os.open(os.sep, _directory_open_flags()) if root_fd is None else os.dup(root_fd)
    try:
        for component in parent_components:
            if component in {"", ".", ".."}:
                raise OSError(f"unsafe local path component: {path}")
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    os.mkdir(component, 0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, components[-1]
    except BaseException:
        os.close(current_fd)
        raise


def _lstat_for_nofollow_compat(
    parent_fd: int,
    name: str,
    path: str,
) -> os.stat_result | None:
    """Capture the final component when the platform lacks ``O_NOFOLLOW``."""
    if getattr(os, "O_NOFOLLOW", 0):
        return None
    result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise OSError(f"refusing to open symbolic link: {path}")
    return result


def _verify_opened_regular_file(
    fd: int,
    path: str,
    before: os.stat_result | None,
) -> None:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(f"refusing to access non-regular file: {path}")
    if before is not None and (opened.st_dev != before.st_dev or opened.st_ino != before.st_ino):
        raise OSError(f"file changed while opening without O_NOFOLLOW support: {path}")


def _verify_path_still_names_open_file(
    parent_fd: int,
    name: str,
    fd: int,
    path: str,
) -> None:
    opened = os.fstat(fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
        raise OSError(f"local file path changed during access: {path}")


def _sync_read_regular_file(path: str, limit: int, root_fd: int | None = None) -> bytes:
    parent_fd, name = _open_parent_dirfd(
        path,
        create_parents=False,
        root_fd=root_fd,
    )
    try:
        before = _lstat_for_nofollow_compat(parent_fd, name, path)
        fd = os.open(
            name,
            _open_regular_file_flags(os.O_RDONLY),
            dir_fd=parent_fd,
        )
        try:
            _verify_opened_regular_file(fd, path, before)
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limit:
                raise OSError(f"local file exceeds read limit of {limit} bytes: {path}")
            _verify_path_still_names_open_file(parent_fd, name, fd, path)
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _sync_write_regular_file(
    path: str,
    payload: bytes,
    root_fd: int | None = None,
) -> None:
    parent_fd, name = _open_parent_dirfd(
        path,
        create_parents=True,
        root_fd=root_fd,
    )
    temporary = ""
    fd = -1
    identity: tuple[int, int] | None = None
    primary_error: BaseException | None = None
    try:
        if name.startswith(RETIRED_FILE_PREFIX):
            raise ValueError(
                f"local file path uses the reserved retirement namespace: {path}"
            )
        try:
            initial_target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            initial_target = None
        if initial_target is not None and not stat.S_ISREG(initial_target.st_mode):
            raise OSError(f"refusing to replace non-regular local file: {path}")
        target_mode = (
            stat.S_IMODE(initial_target.st_mode)
            if initial_target is not None
            else 0o666
        )
        temporary, fd, identity, _candidate_budget = create_owned_retirement_candidate(
            parent_fd,
            mode=target_mode,
            required_files=2 if initial_target is not None else 1,
            required_bytes=(
                max(initial_target.st_size, 0)
                if initial_target is not None
                else 0
            ),
            candidate_bytes=len(payload),
            path_label=path,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset : offset + 65_536])
            if written <= 0:
                raise OSError(f"short write while writing local file: {path}")
            offset += written
        os.ftruncate(fd, len(payload))
        if initial_target is not None:
            os.fchmod(fd, stat.S_IMODE(initial_target.st_mode))
        os.fsync(fd)
        _verify_path_still_names_open_file(parent_fd, temporary, fd, path)
        try:
            commit_owned_temporary(
                parent_fd,
                temporary,
                name,
                fd,
                identity,
                path_label=path,
                expected_target_identity=(
                    (initial_target.st_dev, initial_target.st_ino)
                    if initial_target is not None
                    else None
                ),
                require_target_absent=initial_target is None,
            )
        except FileExistsError as error:
            raise OSError(f"local file appeared before atomic replace: {path}") from error
        except OSError as error:
            if (
                initial_target is not None
                and "atomic target identity changed before commit" in str(error)
            ):
                raise OSError(
                    f"local file changed before atomic replace: {path}"
                ) from error
            raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        if temporary and fd >= 0 and identity is not None:
            try:
                quarantine_unlink_owned_file(
                    parent_fd,
                    temporary,
                    fd,
                    identity,
                    path_label=path,
                )
            except FileNotFoundError:
                pass
            except BaseException as exc:
                cleanup_errors.append(("temporary cleanup", exc))
        if fd >= 0:
            try:
                os.close(fd)
            except BaseException as exc:
                cleanup_errors.append(("temporary fd close", exc))
        try:
            os.close(parent_fd)
        except BaseException as exc:
            cleanup_errors.append(("parent fd close", exc))
        if cleanup_errors:
            if primary_error is not None:
                for stage, cleanup_error in cleanup_errors:
                    add_exception_note(
                        primary_error,
                        f"local atomic write {stage} failed with "
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                    )
            else:
                stage, cleanup_error = cleanup_errors[0]
                add_exception_note(cleanup_error, f"local atomic write cleanup stage: {stage}")
                raise cleanup_error


def _sync_unlink_file(
    path: str,
    expected_identity: tuple[int, int] | _TemporaryFileOwnership | None,
    root_fd: int | None = None,
) -> None:
    parent_fd, name = _open_parent_dirfd(
        path,
        create_parents=False,
        root_fd=root_fd,
    )
    try:
        if expected_identity is None:
            raise _TemporaryFileReplacedError(
                f"refusing to remove local file without temporary ownership proof: {path}"
            )
        if isinstance(expected_identity, _TemporaryFileOwnership):
            with expected_identity.lock:
                if expected_identity.closed:
                    try:
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        return
                    raise _TemporaryFileReplacedError(
                        f"refusing to remove replaced local temporary file: {path}"
                    )
                expected = (expected_identity.dev, expected_identity.ino)
                try:
                    quarantine_unlink_owned_file(
                        parent_fd,
                        name,
                        expected_identity.fd,
                        expected,
                        path_label=path,
                    )
                except OwnedFileMismatchError as exc:
                    raise _TemporaryFileReplacedError(
                        f"refusing to remove replaced local temporary file: {path}"
                    ) from exc
                os.close(expected_identity.fd)
                expected_identity.closed = True
            return
        raise _TemporaryFileReplacedError(
            f"refusing to remove local file without live temporary ownership proof: {path}"
        )
    finally:
        os.close(parent_fd)


def _sync_create_temp_file(
    temp_dir: str,
    prefix: str,
    suffix: str,
    payload: bytes,
) -> tuple[str, _TemporaryFileOwnership]:
    canonical_temp_dir = os.path.realpath(temp_dir)
    parent_fd, directory_name = _open_parent_dirfd(
        canonical_temp_dir,
        create_parents=False,
    )
    try:
        directory_fd = os.open(
            directory_name,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        for candidate_part in tempfile._get_candidate_names():
            candidate = f"{prefix}{candidate_part}{suffix}"
            try:
                fd = os.open(
                    candidate,
                    _open_regular_file_flags(os.O_WRONLY) | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset : offset + 65_536])
                    if written <= 0:
                        raise OSError("short write while staging temporary file")
                    offset += written
                os.fsync(fd)
                _verify_path_still_names_open_file(
                    directory_fd,
                    candidate,
                    fd,
                    os.path.join(canonical_temp_dir, candidate),
                )
                opened = os.fstat(fd)
            except BaseException as original:
                try:
                    opened = os.fstat(fd)
                    quarantine_unlink_owned_file(
                        directory_fd,
                        candidate,
                        fd,
                        (opened.st_dev, opened.st_ino),
                        path_label=os.path.join(canonical_temp_dir, candidate),
                    )
                except BaseException as cleanup_error:
                    add_exception_note(
                        original,
                        "local temporary creation cleanup failed with "
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                    )
                raise
            else:
                ownership = _TemporaryFileOwnership(
                    fd=fd,
                    dev=opened.st_dev,
                    ino=opened.st_ino,
                )
                fd = -1
                return os.path.join(canonical_temp_dir, candidate), ownership
            finally:
                if fd >= 0:
                    os.close(fd)
        raise FileExistsError("could not allocate exclusive local temporary file")
    finally:
        os.close(directory_fd)


async def _await_owned_transaction(awaitable, *, failure_note: str):
    """Finish every stage of an owned transaction before propagating cancel."""
    worker = asyncio.ensure_future(awaitable)
    first_cancellation: asyncio.CancelledError | None = None
    operation_error: BaseException | None = None
    result: object = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                if first_cancellation is None:
                    raise
                operation_error = exc
                break
            if first_cancellation is None:
                first_cancellation = exc
        except BaseException as exc:
            operation_error = exc
            break

    if first_cancellation is not None:
        if operation_error is not None:
            add_exception_note(
                first_cancellation,
                f"{failure_note} failed after cancellation: {type(operation_error).__name__}: {operation_error}",
            )
        raise first_cancellation
    if operation_error is not None:
        raise operation_error
    return result


async def _run_owned_blocking_io(operation: Callable[..., object], *args: object):
    """Keep blocking host I/O off the loop and finish owned writes on cancel."""
    return await _await_owned_transaction(
        asyncio.to_thread(operation, *args),
        failure_note="owned file operation",
    )
