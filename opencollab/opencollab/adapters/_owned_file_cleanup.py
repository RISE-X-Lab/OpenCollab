"""Fail-closed retirement for adapter-owned filesystem entries."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from opencollab.adapters import _atomic_rename
from opencollab.adapters._posix_file_support import require_posix_file_support
from opencollab.adapters.retirement_registry import (
    RETIRED_FILE_PREFIX,
    register_verified_retirement,
)
from opencollab.application.exception_notes import add_exception_note

MAX_RETIRED_FILES_PER_DIRECTORY = 256
MAX_RETIRED_BYTES_PER_DIRECTORY = 1024 * 1024 * 1024


class OwnedFileRetirementError(OSError):
    """A post-rename retirement step failed with the tombstone retained."""

    def __init__(
        self,
        message: str,
        *,
        retired_name: str | None = None,
        identity_verified: bool = False,
    ) -> None:
        super().__init__(message)
        self.retired_name = retired_name
        self.identity_verified = identity_verified


class OwnedFileMismatchError(OwnedFileRetirementError):
    """A retired path did not match its live ownership descriptor."""


@contextmanager
def retirement_lock(parent_fd: int) -> Iterator[None]:
    """Serialize cooperative retirements and their directory quota checks."""
    fcntl = require_posix_file_support()
    fcntl.flock(parent_fd, fcntl.LOCK_EX)
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        except BaseException as unlock_error:
            if primary_error is None:
                raise
            add_exception_note(
                primary_error,
                "retirement lock release failed with "
                f"{type(unlock_error).__name__}: {unlock_error}",
            )


def _retired_usage(parent_fd: int) -> tuple[int, int]:
    count = 0
    total_bytes = 0
    for name in os.listdir(parent_fd):
        if not name.startswith(RETIRED_FILE_PREFIX):
            continue
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        count += 1
        total_bytes += max(entry.st_size, 0)
    return count, total_bytes


def require_retirement_capacity(
    parent_fd: int,
    *,
    additional_files: int,
    additional_bytes: int,
    path_label: str,
) -> None:
    """Require room for a complete cooperative retirement transaction."""
    require_posix_file_support()
    if (
        isinstance(additional_files, bool)
        or not isinstance(additional_files, int)
        or isinstance(additional_bytes, bool)
        or not isinstance(additional_bytes, int)
    ):
        raise TypeError("retirement capacity reservation must use integers")
    if additional_files < 0 or additional_bytes < 0:
        raise ValueError("retirement capacity reservation cannot be negative")
    retired_count, retired_bytes = _retired_usage(parent_fd)
    if retired_count + additional_files > MAX_RETIRED_FILES_PER_DIRECTORY:
        raise OSError(
            f"retired-file count limit reached for parent of {path_label}: "
            f"{retired_count} existing, {additional_files} required"
        )
    if retired_bytes + additional_bytes > MAX_RETIRED_BYTES_PER_DIRECTORY:
        raise OSError(
            f"retired-file byte limit reached for parent of {path_label}: "
            f"{retired_bytes} existing, {additional_bytes} required bytes"
        )


def _check_retirement_capacity(parent_fd: int, size: int, path_label: str) -> None:
    require_retirement_capacity(
        parent_fd,
        additional_files=1,
        additional_bytes=max(size, 0),
        path_label=path_label,
    )


def _new_retired_name() -> str:
    return f"{RETIRED_FILE_PREFIX}{uuid.uuid4().hex}"


def _retire_entry_locked(
    parent_fd: int,
    name: str,
    *,
    path_label: str,
) -> str | None:
    for _attempt in range(20):
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _check_retirement_capacity(parent_fd, current.st_size, path_label)
        retired = _new_retired_name()
        try:
            _atomic_rename.rename_noreplace(
                name,
                retired,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except FileNotFoundError:
            return None
        return retired
    raise OSError(f"could not allocate a no-clobber retired-file name for {path_label}")


def _sync_retirement(
    parent_fd: int,
    retired: str,
    path_label: str,
    *,
    identity_verified: bool = False,
) -> None:
    try:
        os.fsync(parent_fd)
    except OSError as error:
        raise OwnedFileRetirementError(
            f"retirement directory sync failed for {path_label}: {error}; "
            f"entry retained as {retired}",
            retired_name=retired,
            identity_verified=identity_verified,
        ) from error


def _register_retirement(
    parent_fd: int,
    retired: str,
    path_label: str,
) -> None:
    try:
        register_verified_retirement(parent_fd, retired)
    except BaseException as error:
        raise OwnedFileRetirementError(
            f"verified retirement registration failed for {path_label}: {error}; "
            f"entry retained as {retired}",
            retired_name=retired,
            identity_verified=True,
        ) from error


def create_owned_retirement_candidate(
    parent_fd: int,
    *,
    mode: int,
    required_files: int,
    required_bytes: int,
    candidate_bytes: int,
    path_label: str,
) -> tuple[str, int, tuple[int, int], int]:
    """Create an owned candidate inside the bounded retirement namespace."""
    require_posix_file_support()
    if isinstance(candidate_bytes, bool) or not isinstance(candidate_bytes, int):
        raise TypeError("retirement candidate byte reservation must be an integer")
    if candidate_bytes < 0:
        raise ValueError("retirement candidate byte reservation cannot be negative")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    candidate_fd = -1
    candidate_name: str | None = None
    candidate_identity: tuple[int, int] | None = None
    result: tuple[str, int, tuple[int, int], int] | None = None
    try:
        with retirement_lock(parent_fd):
            require_retirement_capacity(
                parent_fd,
                additional_files=required_files,
                additional_bytes=required_bytes + candidate_bytes,
                path_label=path_label,
            )
            candidate_budget = candidate_bytes
            for _attempt in range(20):
                name = _new_retired_name()
                try:
                    candidate_fd = os.open(name, flags, mode, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                candidate_name = name
                opened = os.fstat(candidate_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise OSError(f"retirement candidate is not regular: {path_label}")
                candidate_identity = (opened.st_dev, opened.st_ino)
                try:
                    os.ftruncate(candidate_fd, candidate_budget)
                except OSError as truncate_error:
                    try:
                        os.ftruncate(candidate_fd, candidate_budget)
                    except OSError as retry_error:
                        add_exception_note(
                            truncate_error,
                            "retirement candidate reservation retry failed with "
                            f"{type(retry_error).__name__}: {retry_error}",
                        )
                        raise truncate_error
                result = (
                    name,
                    candidate_fd,
                    candidate_identity,
                    candidate_budget,
                )
                break
        if result is None:
            raise OSError(f"could not allocate a retirement candidate for {path_label}")
        candidate_fd = -1
        return result
    except BaseException as error:
        if candidate_fd >= 0:
            if candidate_name is not None:
                if candidate_identity is None:
                    try:
                        opened = os.fstat(candidate_fd)
                        if stat.S_ISREG(opened.st_mode):
                            candidate_identity = (opened.st_dev, opened.st_ino)
                    except BaseException as identity_error:
                        add_exception_note(
                            error,
                            "retirement candidate identity recovery failed with "
                            f"{type(identity_error).__name__}: {identity_error}",
                        )
                if candidate_identity is not None:
                    try:
                        finalize_owned_retirement_candidate(
                            parent_fd,
                            candidate_name,
                            candidate_fd,
                            candidate_identity,
                            path_label=path_label,
                        )
                    except BaseException as finalize_error:
                        add_exception_note(
                            error,
                            "retirement candidate recovery failed with "
                            f"{type(finalize_error).__name__}: {finalize_error}",
                        )
            try:
                os.close(candidate_fd)
            except BaseException as close_error:
                add_exception_note(
                    error,
                    "retirement candidate fd close failed with "
                    f"{type(close_error).__name__}: {close_error}",
                )
        raise


def finalize_owned_retirement_candidate(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
    lock_held: bool = False,
) -> str:
    """Verify, bound, sync, and register an in-place owned tombstone."""
    require_posix_file_support()
    if not name.startswith(RETIRED_FILE_PREFIX):
        raise ValueError("retirement candidate is outside the reserved namespace")

    def finalize() -> str:
        opened = os.fstat(owned_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != expected_identity
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            raise OwnedFileMismatchError(
                f"retirement candidate no longer matches owned file: {path_label}",
                retired_name=name,
            )
        retired_count, retired_bytes = _retired_usage(parent_fd)
        if retired_bytes > MAX_RETIRED_BYTES_PER_DIRECTORY:
            os.ftruncate(owned_fd, 0)
            os.fsync(owned_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != expected_identity:
                raise OwnedFileMismatchError(
                    f"retirement candidate changed while bounding: {path_label}",
                    retired_name=name,
                )
            retired_count, bytes_after = _retired_usage(parent_fd)
            if bytes_after > MAX_RETIRED_BYTES_PER_DIRECTORY:
                raise OSError(
                    f"retired-file byte limit remains exceeded after bounding {path_label}: "
                    f"{bytes_after}"
                )
        if retired_count > MAX_RETIRED_FILES_PER_DIRECTORY:
            raise OSError(
                f"retired-file count limit exceeded while finalizing {path_label}: "
                f"{retired_count}"
            )
        _sync_retirement(
            parent_fd,
            name,
            path_label,
            identity_verified=True,
        )
        _register_retirement(parent_fd, name, path_label)
        return name

    if lock_held:
        return finalize()
    with retirement_lock(parent_fd):
        return finalize()


def refresh_verified_retirement_record(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
    lock_held: bool = False,
) -> None:
    """Refresh registry metadata after a verified tombstone gains a hardlink."""

    def refresh() -> None:
        opened = os.fstat(owned_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != expected_identity
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            raise OwnedFileMismatchError(
                f"retirement record refresh identity mismatch: {path_label}",
                retired_name=name,
            )
        _sync_retirement(
            parent_fd,
            name,
            path_label,
            identity_verified=True,
        )
        _register_retirement(parent_fd, name, path_label)

    if lock_held:
        refresh()
        return
    with retirement_lock(parent_fd):
        refresh()


def retire_unverified_file(
    parent_fd: int,
    name: str,
    *,
    path_label: str,
    lock_held: bool = False,
) -> str | None:
    """Retire the current no-follow entry without ever deleting its inode."""
    require_posix_file_support()
    if lock_held:
        retired = _retire_entry_locked(
            parent_fd,
            name,
            path_label=path_label,
        )
        if retired is not None:
            _sync_retirement(parent_fd, retired, path_label)
        return retired
    with retirement_lock(parent_fd):
        retired = _retire_entry_locked(
            parent_fd,
            name,
            path_label=path_label,
        )
        if retired is not None:
            _sync_retirement(parent_fd, retired, path_label)
        return retired


def retire_owned_file(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
    lock_held: bool = False,
) -> str | None:
    """Retire a name and prove the tombstone through a pinned descriptor."""
    require_posix_file_support()
    opened = os.fstat(owned_fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != expected_identity
    ):
        raise OwnedFileMismatchError(
            f"refusing retirement with invalid ownership descriptor: {path_label}"
        )

    def retire_and_verify() -> str | None:
        retired = _retire_entry_locked(
            parent_fd,
            name,
            path_label=path_label,
        )
        if retired is None:
            return None
        try:
            current = os.stat(retired, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            try:
                _sync_retirement(parent_fd, retired, path_label)
            except OwnedFileRetirementError as sync_error:
                add_exception_note(error, str(sync_error))
            raise OwnedFileRetirementError(
                f"retired entry verification failed for {path_label}: {error}; "
                f"entry expected as {retired}",
                retired_name=retired,
            ) from error
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            try:
                _atomic_rename.rename_noreplace(
                    retired,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except FileExistsError:
                mismatch = OwnedFileMismatchError(
                    f"retired entry does not match owned file: {path_label}; "
                    f"canonical successor already exists and entry remains as {retired}",
                    retired_name=retired,
                )
                try:
                    _sync_retirement(parent_fd, retired, path_label)
                except OwnedFileRetirementError as sync_error:
                    add_exception_note(mismatch, str(sync_error))
            except BaseException as restore_error:
                mismatch = OwnedFileMismatchError(
                    f"retired entry does not match owned file: {path_label}; "
                    f"canonical restore failed and entry remains as {retired}",
                    retired_name=retired,
                )
                add_exception_note(
                    mismatch,
                    "canonical successor restore failed with "
                    f"{type(restore_error).__name__}: {restore_error}",
                )
                try:
                    _sync_retirement(parent_fd, retired, path_label)
                except OwnedFileRetirementError as sync_error:
                    add_exception_note(mismatch, str(sync_error))
            else:
                mismatch = OwnedFileMismatchError(
                    f"retired entry does not match owned file: {path_label}; "
                    "canonical successor restored",
                )
                try:
                    os.fsync(parent_fd)
                except BaseException as sync_error:
                    add_exception_note(
                        mismatch,
                        "canonical successor restore sync failed with "
                        f"{type(sync_error).__name__}: {sync_error}",
                    )
            raise mismatch
        _sync_retirement(
            parent_fd,
            retired,
            path_label,
            identity_verified=True,
        )
        _register_retirement(parent_fd, retired, path_label)
        return retired

    if lock_held:
        return retire_and_verify()
    with retirement_lock(parent_fd):
        return retire_and_verify()


def quarantine_unlink_owned_file(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
) -> bool:
    """Compatibility wrapper: retirement replaces unsafe name-based unlink."""
    if name.startswith(RETIRED_FILE_PREFIX):
        finalize_owned_retirement_candidate(
            parent_fd,
            name,
            owned_fd,
            expected_identity,
            path_label=path_label,
        )
        return True
    return retire_owned_file(
        parent_fd,
        name,
        owned_fd,
        expected_identity,
        path_label=path_label,
    ) is not None


__all__ = [
    "MAX_RETIRED_BYTES_PER_DIRECTORY",
    "MAX_RETIRED_FILES_PER_DIRECTORY",
    "OwnedFileMismatchError",
    "OwnedFileRetirementError",
    "RETIRED_FILE_PREFIX",
    "create_owned_retirement_candidate",
    "finalize_owned_retirement_candidate",
    "quarantine_unlink_owned_file",
    "refresh_verified_retirement_record",
    "require_retirement_capacity",
    "retire_owned_file",
    "retire_unverified_file",
    "retirement_lock",
]
