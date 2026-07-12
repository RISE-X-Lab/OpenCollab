"""Descriptor-safe atomic writes and durable regular-file retirement."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from opencollab.adapters import _safe_file_support as support
from opencollab.adapters._atomic_file_commit import commit_owned_temporary
from opencollab.adapters._bounded_candidate_writer import write_bounded_candidate
from opencollab.adapters._owned_file_cleanup import (
    RETIRED_FILE_PREFIX,
    create_owned_retirement_candidate,
    quarantine_unlink_owned_file,
)
from opencollab.application.exception_notes import add_exception_note


@dataclass
class _AtomicState:
    parent_fd: int
    temporary: str = ""
    fd: int = -1
    temporary_identity: tuple[int, int] | None = None


def verify_atomic_parent_binding(
    target: Path,
    parent_fd: int,
    *,
    context: str,
    phase: str,
    operation: str,
    expected_target_identity: tuple[int, int] | None = None,
) -> None:
    verified_fd = support.open_directory_no_symlinks(target.parent)
    verification_error: BaseException | None = None
    try:
        original = os.fstat(parent_fd)
        verified = os.fstat(verified_fd)
        if (original.st_dev, original.st_ino) != (verified.st_dev, verified.st_ino):
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
        if verification_error is None:
            raise
        add_exception_note(
            verification_error,
            f"{context} verified parent fd close failed with "
            f"{type(close_error).__name__}: {close_error}",
        )
    if verification_error is not None:
        raise verification_error


def _validate_atomic_request(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    expected_target_identity: tuple[int, int] | None,
    require_target_absent: bool,
    context: str,
) -> Path:
    target = Path(os.path.abspath(path))
    if not target.name or target.name in {".", ".."}:
        raise ValueError(f"invalid {context} path: {path}")
    if target.name.startswith(RETIRED_FILE_PREFIX):
        raise ValueError(f"{context} path uses the reserved retirement namespace: {path}")
    if expected_target_identity is not None and require_target_absent:
        raise ValueError(
            "expected_target_identity and require_target_absent are mutually exclusive"
        )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    return target


def _require_expected_parent(
    target: Path,
    parent_fd: int,
    *,
    expected_parent_identity: tuple[int, int] | None,
    context: str,
) -> None:
    if expected_parent_identity is None:
        return
    parent_info = os.fstat(parent_fd)
    if (parent_info.st_dev, parent_info.st_ino) != expected_parent_identity:
        raise OSError(f"{context} parent identity changed: {target.parent}")


def _inspect_initial_target(
    target: Path,
    parent_fd: int,
    *,
    expected_target_identity: tuple[int, int] | None,
    require_target_absent: bool,
    create_only: bool,
    context: str,
) -> tuple[os.stat_result | None, tuple[int, int] | None]:
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
    return initial_target, initial_identity


def _prepare_candidate(
    state: _AtomicState,
    target: Path,
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    mode: int,
    initial_target: os.stat_result | None,
    context: str,
) -> None:
    state.temporary, state.fd, state.temporary_identity, candidate_budget = (
        create_owned_retirement_candidate(
            state.parent_fd,
            mode=mode,
            required_files=2 if initial_target is not None else 1,
            required_bytes=(
                max(initial_target.st_size, 0) if initial_target is not None else 0
            ),
            candidate_bytes=max_bytes,
            path_label=str(target),
        )
    )
    write_bounded_candidate(
        state.fd,
        writer,
        max_bytes=candidate_budget,
        context=context,
    )
    os.fsync(state.fd)


def _require_stable_candidate(
    state: _AtomicState,
    target: Path,
    *,
    context: str,
    operation: str,
) -> None:
    written = os.fstat(state.fd)
    if (
        not stat.S_ISREG(written.st_mode)
        or (written.st_dev, written.st_ino) != state.temporary_identity
    ):
        raise OSError(f"{context} temporary identity changed: {target}")
    current = os.stat(
        state.temporary,
        dir_fd=state.parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != state.temporary_identity
    ):
        raise OSError(f"{context} temporary path changed before {operation}: {target}")


def _commit_candidate(
    state: _AtomicState,
    target: Path,
    *,
    initial_identity: tuple[int, int] | None,
    effective_create: bool,
    create_only: bool,
    context: str,
) -> None:
    operation = "create" if effective_create else "replace"
    verify_atomic_parent_binding(
        target,
        state.parent_fd,
        context=context,
        phase="before",
        operation=operation,
    )
    commit_owned_temporary(
        state.parent_fd,
        state.temporary,
        target.name,
        state.fd,
        state.temporary_identity,
        path_label=str(target),
        expected_target_identity=initial_identity,
        require_target_absent=effective_create,
        create_only=create_only,
        post_commit_check=lambda: verify_atomic_parent_binding(
            target,
            state.parent_fd,
            context=context,
            phase="after",
            operation=operation,
            expected_target_identity=state.temporary_identity,
        ),
    )


def _retire_temporary(state: _AtomicState, target: Path, context: str) -> None:
    if not state.temporary:
        return
    if state.fd < 0 or state.temporary_identity is None:
        try:
            os.stat(
                state.temporary,
                dir_fd=state.parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise OSError(f"refusing to retire {context} temporary path without ownership proof")
    try:
        quarantine_unlink_owned_file(
            state.parent_fd,
            state.temporary,
            state.fd,
            state.temporary_identity,
            path_label=str(target),
        )
    except FileNotFoundError:
        return


def _capture_cleanup_error(
    cleanup_errors: list[tuple[str, BaseException]],
    stage: str,
    cleanup: Callable[[], None],
) -> None:
    try:
        cleanup()
    except BaseException as exc:
        cleanup_errors.append((stage, exc))


def _report_cleanup_errors(
    cleanup_errors: list[tuple[str, BaseException]],
    primary_error: BaseException | None,
) -> None:
    if not cleanup_errors:
        return
    if primary_error is not None:
        for stage, cleanup_error in cleanup_errors:
            add_exception_note(
                primary_error,
                f"atomic cleanup {stage} failed with "
                f"{type(cleanup_error).__name__}: {cleanup_error}",
            )
        return
    stage, cleanup_error = cleanup_errors[0]
    for extra_stage, extra_error in cleanup_errors[1:]:
        add_exception_note(
            cleanup_error,
            f"additional atomic cleanup {extra_stage} failed with "
            f"{type(extra_error).__name__}: {extra_error}",
        )
    add_exception_note(cleanup_error, f"atomic cleanup stage: {stage}")
    raise cleanup_error


def _cleanup_atomic_state(
    state: _AtomicState,
    target: Path,
    *,
    context: str,
    primary_error: BaseException | None,
) -> None:
    cleanup_errors: list[tuple[str, BaseException]] = []
    _capture_cleanup_error(
        cleanup_errors,
        f"temporary retirement {state.temporary}",
        lambda: _retire_temporary(state, target, context),
    )
    if state.fd >= 0:
        _capture_cleanup_error(
            cleanup_errors,
            "temporary fd close",
            lambda: os.close(state.fd),
        )
    _capture_cleanup_error(
        cleanup_errors,
        "parent directory fd close",
        lambda: os.close(state.parent_fd),
    )
    _report_cleanup_errors(cleanup_errors, primary_error)


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
    _ensure_directory: Callable[[str | os.PathLike[str]], None] = (
        support.ensure_directory_no_symlinks
    ),
) -> None:
    """Write and durably replace one regular file while retaining temp ownership."""
    target = _validate_atomic_request(
        path,
        max_bytes=max_bytes,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        context=context,
    )
    _ensure_directory(target.parent)
    state = _AtomicState(support.open_directory_no_symlinks(target.parent))
    primary_error: BaseException | None = None
    try:
        _require_expected_parent(
            target,
            state.parent_fd,
            expected_parent_identity=expected_parent_identity,
            context=context,
        )
        initial_target, initial_identity = _inspect_initial_target(
            target,
            state.parent_fd,
            expected_target_identity=expected_target_identity,
            require_target_absent=require_target_absent,
            create_only=create_only,
            context=context,
        )
        effective_create = initial_target is None
        _prepare_candidate(
            state,
            target,
            writer,
            max_bytes=max_bytes,
            mode=mode,
            initial_target=initial_target,
            context=context,
        )
        operation = "create" if effective_create else "replace"
        _require_stable_candidate(state, target, context=context, operation=operation)
        _commit_candidate(
            state,
            target,
            initial_identity=initial_identity,
            effective_create=effective_create,
            create_only=create_only,
            context=context,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _cleanup_atomic_state(
            state,
            target,
            context=context,
            primary_error=primary_error,
        )


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
    _write_file: Callable[..., None] = write_regular_file_atomic,
) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("atomic payload must be bytes")
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0
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

    _write_file(
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


def _stat_retirement_target(target: Path, parent_fd: int) -> os.stat_result | None:
    try:
        before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"retirement target is not a regular file: {target}")
    return before


def _open_retirement_target(
    target: Path,
    parent_fd: int,
    before: os.stat_result,
    expected_target_identity: tuple[int, int] | None,
) -> tuple[int, tuple[int, int]] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if not stat.S_ISREG(opened.st_mode) or identity != (
            before.st_dev,
            before.st_ino,
        ):
            raise OSError(f"retirement target changed while opening: {target}")
        if expected_target_identity is not None and identity != expected_target_identity:
            raise OSError(f"retirement target identity changed: {target}")
    except BaseException:
        os.close(fd)
        raise
    return fd, identity


def unlink_regular_file_durable(
    path: str | os.PathLike[str],
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
) -> bool:
    """Retire one descriptor-pinned regular file under a unique name."""
    target = Path(os.path.abspath(path))
    parent_fd = support.open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        _require_expected_parent(
            target,
            parent_fd,
            expected_parent_identity=expected_parent_identity,
            context="retirement target",
        )
        before = _stat_retirement_target(target, parent_fd)
        if before is None:
            return False
        opened = _open_retirement_target(
            target,
            parent_fd,
            before,
            expected_target_identity,
        )
        if opened is None:
            return False
        fd, identity = opened
        if not support.directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"retirement target parent changed: {target.parent}")
        retired = quarantine_unlink_owned_file(
            parent_fd,
            target.name,
            fd,
            identity,
            path_label=str(target),
        )
        if not support.directory_path_matches_fd(target.parent, parent_fd):
            raise OSError(f"retirement target parent changed: {target.parent}")
        return retired
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


__all__ = [
    "create_regular_bytes_atomic",
    "unlink_regular_file_durable",
    "verify_atomic_parent_binding",
    "write_regular_bytes_atomic",
    "write_regular_file_atomic",
]
