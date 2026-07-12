"""Public facade for descriptor-safe regular-file operations."""

from __future__ import annotations

import os
from typing import BinaryIO, Callable

from opencollab.adapters import _safe_file_atomic as _atomic
from opencollab.adapters import _safe_file_read_append as _read_append
from opencollab.adapters import _safe_file_support as _support
from opencollab.adapters._safe_file_atomic import (
    unlink_regular_file_durable,
)
from opencollab.adapters._safe_file_read_append import (
    open_regular_text_append,
    read_regular_bytes,
    regular_handle_matches_path,
    regular_path_identity,
    write_locked_text,
)
from opencollab.adapters._safe_file_support import ensure_directory_no_symlinks

# Private compatibility aliases retained for existing diagnostic integrations.
_acquire_append_lock = _read_append._acquire_append_lock
_directory_path_matches_fd = _support.directory_path_matches_fd
_open_directory_no_symlinks = _support.open_directory_no_symlinks
_verify_atomic_parent_binding = _atomic.verify_atomic_parent_binding
_write_regular_bytes_atomic = _atomic._write_regular_bytes_atomic


def read_regular_text(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    """Read text through the facade's current byte-reader binding."""
    return read_regular_bytes(path, max_bytes=max_bytes).decode(encoding)


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
    """Write atomically through the facade's current directory helper."""
    _atomic.write_regular_file_atomic(
        path,
        writer,
        max_bytes=max_bytes,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        context=context,
        create_only=create_only,
        _ensure_directory=ensure_directory_no_symlinks,
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
    """Write bytes through the facade's current atomic-writer binding."""
    _atomic._write_regular_bytes_atomic(
        path,
        payload,
        max_bytes=max_bytes,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        create_only=False,
        _write_file=write_regular_file_atomic,
    )


def create_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Create bytes through the facade's current atomic-writer binding."""
    _atomic._write_regular_bytes_atomic(
        path,
        payload,
        max_bytes=max_bytes,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        require_target_absent=True,
        create_only=True,
        _write_file=write_regular_file_atomic,
    )


def append_regular_text(path: str | os.PathLike[str], text: str) -> None:
    """Append text while verifying that the locked handle remains path-bound."""
    with open_regular_text_append(path) as handle:
        write_locked_text(handle, text)
        if not regular_handle_matches_path(handle, path):
            raise OSError(f"append target changed while writing: {path}")


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
