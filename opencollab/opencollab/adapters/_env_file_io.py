"""Race-resistant local regular-file operations."""

from __future__ import annotations

import asyncio
import math
import os
import stat
import tempfile
from collections.abc import Callable


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


def _open_parent_dirfd(path: str, *, create_parents: bool) -> tuple[int, str]:
    """Resolve every ancestor from ``/`` without following symbolic links."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ValueError("local file path must be absolute text without NUL bytes")
    normalized = os.path.normpath(path)
    components = normalized.split(os.sep)[1:]
    if not components or not components[-1] or components[-1] in {".", ".."}:
        raise OSError(f"local file path has no file component: {path}")
    parent_components = components[:-1]
    current_fd = os.open(os.sep, _directory_open_flags())
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


def _sync_read_regular_file(path: str, limit: int) -> bytes:
    parent_fd, name = _open_parent_dirfd(path, create_parents=False)
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


def _sync_write_regular_file(path: str, payload: bytes) -> None:
    parent_fd, name = _open_parent_dirfd(path, create_parents=True)
    flags = _open_regular_file_flags(os.O_WRONLY)
    try:
        try:
            before = _lstat_for_nofollow_compat(parent_fd, name, path)
        except FileNotFoundError:
            before = None
        if before is None and not getattr(os, "O_NOFOLLOW", 0):
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o666,
                dir_fd=parent_fd,
            )
        else:
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                fd = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o666,
                    dir_fd=parent_fd,
                )
                before = None
        try:
            _verify_opened_regular_file(fd, path, before)
            os.ftruncate(fd, 0)
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset : offset + 65_536])
                if written <= 0:
                    raise OSError(f"short write while writing local file: {path}")
                offset += written
            os.fsync(fd)
            _verify_path_still_names_open_file(parent_fd, name, fd, path)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _sync_unlink_file(path: str, expected_identity: tuple[int, int] | None) -> None:
    parent_fd, name = _open_parent_dirfd(path, create_parents=False)
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            expected_identity is not None
            and (
                current.st_dev,
                current.st_ino,
            )
            != expected_identity
        ):
            raise OSError(f"refusing to remove replaced local temporary file: {path}")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _sync_create_temp_file(
    temp_dir: str,
    prefix: str,
    suffix: str,
    payload: bytes,
) -> tuple[str, tuple[int, int]]:
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
                identity = (opened.st_dev, opened.st_ino)
            except BaseException:
                try:
                    current = os.stat(
                        candidate,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    opened = os.fstat(fd)
                    if (current.st_dev, current.st_ino) == (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        os.unlink(candidate, dir_fd=directory_fd)
                except (FileNotFoundError, OSError):
                    pass
                raise
            finally:
                os.close(fd)
            return os.path.join(canonical_temp_dir, candidate), identity
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
                raise
            if first_cancellation is None:
                first_cancellation = exc
        except BaseException as exc:
            operation_error = exc
            break

    if first_cancellation is not None:
        if operation_error is not None:
            add_note = getattr(first_cancellation, "add_note", None)
            if callable(add_note):
                add_note(
                    f"{failure_note} failed after cancellation: {type(operation_error).__name__}: {operation_error}"
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
