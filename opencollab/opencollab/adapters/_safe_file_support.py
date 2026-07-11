"""Shared path and dirfd helpers for descriptor-safe file operations."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from opencollab.adapters._posix_file_support import (
    normalize_trusted_root_alias,
    require_posix_file_support,
)


def open_directory_no_symlinks(path: Path) -> int:
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
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                os.close(next_fd)
                raise OSError(f"directory parent changed while opening: {absolute}")
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def directory_path_matches_fd(path: Path, fd: int) -> bool:
    verified_fd = open_directory_no_symlinks(path)
    try:
        original = os.fstat(fd)
        verified = os.fstat(verified_fd)
        return (original.st_dev, original.st_ino) == (
            verified.st_dev,
            verified.st_ino,
        )
    finally:
        os.close(verified_fd)


__all__ = [
    "directory_path_matches_fd",
    "ensure_directory_no_symlinks",
    "open_directory_no_symlinks",
]
