"""Failure-atomic commit of descriptor-pinned temporary files."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable

from opencollab.adapters import _atomic_rename
from opencollab.adapters._owned_file_cleanup import (
    RETIRED_FILE_PREFIX,
    OwnedFileMismatchError,
    OwnedFileRetirementError,
    refresh_verified_retirement_record,
    require_retirement_capacity,
    retire_owned_file,
    retirement_lock,
)
from opencollab.application.exception_notes import add_exception_note

_UNSUPPORTED_RENAME_NOREPLACE_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
)


def _close_pinned_fd(fd: int) -> OSError | None:
    """Close once; an error leaves the descriptor generation unknowable."""
    try:
        os.close(fd)
        return None
    except OSError as close_error:
        return close_error


def _open_regular_at(
    parent_fd: int,
    name: str,
    *,
    path_label: str,
) -> tuple[int, tuple[int, int]] | None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"atomic target is not a regular file: {path_label}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
    except BaseException as error:
        try:
            os.close(fd)
        except BaseException as close_error:
            add_exception_note(
                error,
                "atomic target fd close failed with "
                f"{type(close_error).__name__}: {close_error}",
            )
        raise
    identity = (opened.st_dev, opened.st_ino)
    if not stat.S_ISREG(opened.st_mode) or identity != (before.st_dev, before.st_ino):
        error = OSError(f"atomic target changed while opening: {path_label}")
        try:
            os.close(fd)
        except BaseException as close_error:
            add_exception_note(
                error,
                "atomic target fd close failed with "
                f"{type(close_error).__name__}: {close_error}",
            )
        raise error
    return fd, identity


def _require_owned_name(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
) -> os.stat_result:
    opened = os.fstat(owned_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != expected_identity
        or (current.st_dev, current.st_ino) != expected_identity
    ):
        raise OSError(f"owned temporary path changed before commit: {path_label}")
    return opened


def _recover_failed_commit(
    parent_fd: int,
    target_name: str,
    *,
    candidate_fd: int,
    candidate_name: str,
    candidate_identity: tuple[int, int],
    backup_fd: int,
    backup_name: str | None,
    backup_identity: tuple[int, int] | None,
    path_label: str,
    error: BaseException,
) -> None:
    try:
        current = os.stat(
            target_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        current = None
    except BaseException as recovery_error:
        add_exception_note(
            error,
            "failed commit target inspection failed with "
            f"{type(recovery_error).__name__}: {recovery_error}",
        )
        return
    candidate_is_current = current is not None and (
        stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == candidate_identity
    )
    if backup_identity is not None:
        _recover_failed_exchange(
            parent_fd,
            target_name,
            candidate_fd=candidate_fd,
            candidate_name=candidate_name,
            candidate_identity=candidate_identity,
            backup_fd=backup_fd,
            backup_name=backup_name,
            backup_identity=backup_identity,
            path_label=path_label,
            error=error,
            current=current,
        )
        return
    if current is not None and not candidate_is_current:
        backup_detail = (
            f"; previous target retained as {backup_name}" if backup_name else ""
        )
        add_exception_note(
            error,
            f"concurrent successor preserved at atomic target: {path_label}"
            f"{backup_detail}",
        )
        return
    if current is not None:
        try:
            retire_owned_file(
                parent_fd,
                target_name,
                candidate_fd,
                candidate_identity,
                path_label=f"failed commit target for {path_label}",
                lock_held=True,
            )
        except BaseException as recovery_error:
            add_exception_note(
                error,
                "failed commit target retirement failed with "
                f"{type(recovery_error).__name__}: {recovery_error}",
            )
            if backup_name is None:
                return
            try:
                os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except BaseException as inspection_error:
                add_exception_note(
                    error,
                    "post-retirement target inspection failed with "
                    f"{type(inspection_error).__name__}: {inspection_error}",
                )
                return
            else:
                return


def _entry_identity(
    parent_fd: int,
    name: str,
) -> tuple[int, int] | None:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        return None
    return entry.st_dev, entry.st_ino


def _commit_create_noreplace(
    parent_fd: int,
    temporary_name: str,
    target_name: str,
) -> bool:
    """Commit a create and report whether the owned temporary remains linked."""
    try:
        _atomic_rename.rename_noreplace(
            temporary_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except OSError as error:
        if error.errno not in _UNSUPPORTED_RENAME_NOREPLACE_ERRNOS:
            raise
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        return True
    return False


def _record_recovery_candidate(
    parent_fd: int,
    name: str,
    candidate_name: str,
    candidate_fd: int,
    candidate_identity: tuple[int, int],
    *,
    path_label: str,
    error: BaseException,
) -> None:
    if name == candidate_name:
        return
    try:
        refresh_verified_retirement_record(
            parent_fd,
            name,
            candidate_fd,
            candidate_identity,
            path_label=f"failed replacement candidate for {path_label}",
            lock_held=True,
        )
    except BaseException as recovery_error:
        add_exception_note(
            error,
            "failed replacement candidate registration failed with "
            f"{type(recovery_error).__name__}: {recovery_error}",
        )


def _recover_failed_exchange(
    parent_fd: int,
    target_name: str,
    *,
    candidate_fd: int,
    candidate_name: str,
    candidate_identity: tuple[int, int],
    backup_fd: int,
    backup_name: str | None,
    backup_identity: tuple[int, int],
    path_label: str,
    error: BaseException,
    current: os.stat_result | None,
) -> None:
    current_identity = (
        (current.st_dev, current.st_ino)
        if current is not None and stat.S_ISREG(current.st_mode)
        else None
    )
    if current_identity != candidate_identity:
        if current is not None and current_identity != backup_identity:
            add_exception_note(
                error,
                f"concurrent successor preserved at atomic target: {path_label}",
            )
            if (
                backup_name == candidate_name
                and _entry_identity(parent_fd, backup_name) == backup_identity
            ):
                try:
                    retire_owned_file(
                        parent_fd,
                        backup_name,
                        backup_fd,
                        backup_identity,
                        path_label=f"displaced previous target for {path_label}",
                        lock_held=True,
                    )
                except BaseException as recovery_error:
                    add_exception_note(
                        error,
                        "displaced previous target retirement failed with "
                        f"{type(recovery_error).__name__}: {recovery_error}",
                    )
        if backup_name and _entry_identity(parent_fd, backup_name) == candidate_identity:
            _record_recovery_candidate(
                parent_fd,
                backup_name,
                candidate_name,
                candidate_fd,
                candidate_identity,
                path_label=path_label,
                error=error,
            )
        return
    if backup_name is None or _entry_identity(parent_fd, backup_name) != backup_identity:
        add_exception_note(
            error,
            f"previous target retained only by pinned descriptor after failed replace: {path_label}",
        )
        return
    try:
        _atomic_rename.rename_exchange(
            target_name,
            backup_name,
            first_dir_fd=parent_fd,
            second_dir_fd=parent_fd,
        )
    except BaseException as recovery_error:
        add_exception_note(
            error,
            "atomic replacement rollback exchange failed with "
            f"{type(recovery_error).__name__}: {recovery_error}",
        )
    restored_identity = _entry_identity(parent_fd, target_name)
    retired_candidate_identity = _entry_identity(parent_fd, backup_name)
    if restored_identity != backup_identity or retired_candidate_identity != candidate_identity:
        add_exception_note(
            error,
            f"atomic replacement rollback could not restore prior target: {path_label}",
        )
        return
    try:
        os.fsync(parent_fd)
    except BaseException as recovery_error:
        add_exception_note(
            error,
            "atomic replacement rollback sync failed with "
            f"{type(recovery_error).__name__}: {recovery_error}",
        )
    _record_recovery_candidate(
        parent_fd,
        backup_name,
        candidate_name,
        candidate_fd,
        candidate_identity,
        path_label=path_label,
        error=error,
    )


def commit_owned_temporary(
    parent_fd: int,
    temporary_name: str,
    target_name: str,
    temporary_fd: int,
    temporary_identity: tuple[int, int],
    *,
    path_label: str,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
    create_only: bool = False,
    post_commit_check: Callable[[], None] | None = None,
) -> None:
    """Commit an owned temp while preserving every inode on detected races."""
    backup_fd = -1
    backup_name: str | None = None
    backup_identity: tuple[int, int] | None = None
    commit_attempted = False
    commit_invoked = False
    candidate_alias_retained = False
    primary_error: BaseException | None = None
    effective_create = create_only or require_target_absent
    if not temporary_name.startswith(RETIRED_FILE_PREFIX):
        raise ValueError("atomic temporary must use the bounded retirement namespace")
    with retirement_lock(parent_fd):
        try:
            opened_target = _open_regular_at(
                parent_fd,
                target_name,
                path_label=path_label,
            )
            if opened_target is not None:
                backup_fd, backup_identity = opened_target
            if effective_create and backup_identity is not None:
                raise FileExistsError(f"atomic target appeared before commit: {path_label}")
            if expected_target_identity is not None and backup_identity != expected_target_identity:
                raise OSError(f"atomic target identity changed before commit: {path_label}")
            _require_owned_name(
                parent_fd,
                temporary_name,
                temporary_fd,
                temporary_identity,
                path_label=path_label,
            )
            backup_size = os.fstat(backup_fd).st_size if backup_fd >= 0 else 0
            require_retirement_capacity(
                parent_fd,
                additional_files=1 if backup_identity is not None else 0,
                additional_bytes=max(backup_size, 0),
                path_label=f"atomic commit for {path_label}",
            )
            if backup_identity is not None:
                _require_owned_name(
                    parent_fd,
                    target_name,
                    backup_fd,
                    backup_identity,
                    path_label=path_label,
                )
                backup_name = temporary_name
                commit_invoked = True
                _atomic_rename.rename_exchange(
                    temporary_name,
                    target_name,
                    first_dir_fd=parent_fd,
                    second_dir_fd=parent_fd,
                )
                commit_attempted = True
                if _entry_identity(parent_fd, target_name) != temporary_identity:
                    raise OSError(f"atomic target changed during replace: {path_label}")
                if _entry_identity(parent_fd, temporary_name) != backup_identity:
                    try:
                        _atomic_rename.rename_exchange(
                            target_name,
                            temporary_name,
                            first_dir_fd=parent_fd,
                            second_dir_fd=parent_fd,
                        )
                    except BaseException as rollback_error:
                        raise OwnedFileMismatchError(
                            f"atomic target identity changed during replace: {path_label}"
                        ) from rollback_error
                    raise OwnedFileMismatchError(
                        f"atomic target identity changed during replace: {path_label}"
                    )
                try:
                    backup_name = retire_owned_file(
                        parent_fd,
                        temporary_name,
                        backup_fd,
                        backup_identity,
                        path_label=f"previous target for {path_label}",
                        lock_held=True,
                    )
                except OwnedFileMismatchError:
                    raise
                except OwnedFileRetirementError as error:
                    if error.identity_verified:
                        backup_name = error.retired_name
                    raise
                if backup_name is None:
                    raise OSError(f"atomic target backup disappeared after exchange: {path_label}")
            else:
                commit_invoked = True
                candidate_alias_retained = _commit_create_noreplace(
                    parent_fd,
                    temporary_name,
                    target_name,
                )
            commit_attempted = True
            committed = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(committed.st_mode)
                or (committed.st_dev, committed.st_ino) != temporary_identity
            ):
                operation = "create" if backup_identity is None else "replace"
                raise OSError(f"atomic target changed during {operation}: {path_label}")
            if candidate_alias_retained:
                refresh_verified_retirement_record(
                    parent_fd,
                    temporary_name,
                    temporary_fd,
                    temporary_identity,
                    path_label=f"hard-link commit candidate for {path_label}",
                    lock_held=True,
                )
            os.fsync(parent_fd)
            if post_commit_check is not None:
                post_commit_check()
        except BaseException as error:
            primary_error = error
            if backup_name is not None or commit_attempted or commit_invoked:
                _recover_failed_commit(
                    parent_fd,
                    target_name,
                    candidate_fd=temporary_fd,
                    candidate_name=temporary_name,
                    candidate_identity=temporary_identity,
                    backup_fd=backup_fd,
                    backup_name=backup_name,
                    backup_identity=backup_identity,
                    path_label=path_label,
                    error=error,
                )
            raise
        finally:
            if backup_fd >= 0:
                close_error = _close_pinned_fd(backup_fd)
                if close_error is not None:
                    if primary_error is None:
                        raise close_error
                    add_exception_note(
                        primary_error,
                        "atomic backup fd close failed with "
                        f"{type(close_error).__name__}: {close_error}",
                    )


__all__ = ["commit_owned_temporary"]
