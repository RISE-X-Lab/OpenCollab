"""Stable descriptor-safe file operations for external integrations."""

from __future__ import annotations

from pathlib import Path

from opencollab.adapters._owned_file_cleanup import (
    OwnedFileMismatchError,
    OwnedFileRetirementError,
    quarantine_unlink_owned_file,
)
from opencollab.adapters._safe_file_support import (
    directory_path_matches_fd,
    open_directory_no_symlinks,
)
from opencollab.adapters.safe_files import (
    append_regular_text,
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    open_regular_text_append,
    read_regular_bytes,
    read_regular_text,
    regular_handle_matches_path,
    regular_path_identity,
    unlink_regular_file_durable,
    write_locked_text,
    write_regular_bytes_atomic,
    write_regular_file_atomic,
)


def directory_handle_matches_path(path: Path, directory_fd: int) -> bool:
    """Return whether a directory handle still identifies the visible path."""
    return directory_path_matches_fd(path, directory_fd)


__all__ = [
    "OwnedFileMismatchError",
    "OwnedFileRetirementError",
    "append_regular_text",
    "create_regular_bytes_atomic",
    "directory_handle_matches_path",
    "ensure_directory_no_symlinks",
    "open_directory_no_symlinks",
    "open_regular_text_append",
    "quarantine_unlink_owned_file",
    "read_regular_bytes",
    "read_regular_text",
    "regular_handle_matches_path",
    "regular_path_identity",
    "unlink_regular_file_durable",
    "write_locked_text",
    "write_regular_bytes_atomic",
    "write_regular_file_atomic",
]
