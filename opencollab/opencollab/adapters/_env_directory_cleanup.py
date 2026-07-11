"""Descriptor-pinned directory cleanup helpers."""

from __future__ import annotations

import os
import stat


def _parse_object_id(stdout: str | bytes) -> str:
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    object_id = text.strip()
    if len(object_id) not in {40, 64} or any(
        character not in "0123456789abcdefABCDEF" for character in object_id
    ):
        raise OSError("worktree branch ownership probe returned an invalid object id")
    return object_id.lower()


def _sync_clear_pinned_directory(directory_fd: int, display_path: str) -> None:
    """Remove descendants through a pinned directory without reopening its path."""
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError(f"directory changed during removal: {display_path}/{name}")
                _sync_clear_pinned_directory(child_fd, f"{display_path}/{name}")
                current = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError(f"directory changed during removal: {display_path}/{name}")
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
            continue
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise OSError(f"file changed during removal: {display_path}/{name}")
        os.unlink(name, dir_fd=directory_fd)
