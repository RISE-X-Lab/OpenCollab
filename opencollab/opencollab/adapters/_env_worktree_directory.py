"""Descriptor-pinned directory ownership for Git worktrees."""

from __future__ import annotations

import os
import stat
import uuid

from opencollab.adapters._env_directory_cleanup import _sync_clear_pinned_directory
from opencollab.adapters._env_file_io import _open_parent_dirfd


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    if directory_fd < 0:
        raise OSError("worktree directory identity is unavailable")
    opened = os.fstat(directory_fd)
    if not stat.S_ISDIR(opened.st_mode):
        raise OSError("worktree directory descriptor no longer names a directory")
    return opened.st_dev, opened.st_ino


def _allocate_quarantine_name(parent_fd: int) -> str:
    for _attempt in range(16):
        candidate = f".opencollab-remove-{uuid.uuid4().hex}"
        try:
            os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
    raise FileExistsError("could not allocate worktree quarantine name")


class _OwnedWorktreeDirectoryMixin:
    """Track and remove one worktree directory through a pinned descriptor."""

    def _capture_worktree_directory_handle(self) -> None:
        if self._worktree_dir_fd >= 0 or not self._worktree_dir:
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(self._worktree_dir, flags)
        try:
            opened = os.fstat(fd)
            current = os.lstat(self._worktree_dir)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise OSError("worktree directory changed while recording ownership")
        except BaseException:
            os.close(fd)
            raise
        self._worktree_dir_fd = fd

    def _worktree_directory_state(self, path: str) -> str:
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            return "absent"
        if self._worktree_dir_fd < 0:
            return "unverified"
        opened = os.fstat(self._worktree_dir_fd)
        if (
            stat.S_ISDIR(opened.st_mode)
            and stat.S_ISDIR(current.st_mode)
            and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
        ):
            return "owned"
        return "replaced"

    def _release_worktree_directory_handle(self) -> None:
        if self._worktree_dir_fd >= 0:
            os.close(self._worktree_dir_fd)
            self._worktree_dir_fd = -1

    def _quarantine_owned_worktree_directory(
        self,
        path: str,
        expected: tuple[int, int],
    ) -> str:
        parent_fd, name = _open_parent_dirfd(path, create_parents=False)
        try:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return path
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != expected
            ):
                raise OSError("worktree directory ownership changed before quarantine")
            quarantine_name = _allocate_quarantine_name(parent_fd)
            os.rename(name, quarantine_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            quarantine_path = os.path.join(os.path.dirname(path), quarantine_name)
            self._worktree_quarantine_dir = quarantine_path
            quarantined = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(quarantined.st_mode)
                or (quarantined.st_dev, quarantined.st_ino) != expected
            ):
                self._restore_unverified_quarantine(parent_fd, name, quarantine_name)
                raise OSError("worktree quarantine identity could not be proved")
            return quarantine_path
        finally:
            os.close(parent_fd)

    def _restore_unverified_quarantine(
        self,
        parent_fd: int,
        original_name: str,
        quarantine_name: str,
    ) -> None:
        try:
            os.stat(original_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(
                quarantine_name,
                original_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            self._worktree_quarantine_dir = None

    def _remove_owned_worktree_quarantine(
        self,
        quarantine_path: str,
        expected: tuple[int, int],
    ) -> None:
        parent_fd, quarantine_name = _open_parent_dirfd(
            quarantine_path,
            create_parents=False,
        )
        try:
            try:
                quarantined = os.stat(
                    quarantine_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise OSError("worktree quarantine disappeared before removal") from exc
            if (
                not stat.S_ISDIR(quarantined.st_mode)
                or (quarantined.st_dev, quarantined.st_ino) != expected
            ):
                raise OSError("worktree quarantine identity changed; refusing removal")
            _sync_clear_pinned_directory(self._worktree_dir_fd, quarantine_path)
            self._remove_empty_owned_quarantine(parent_fd, quarantine_name, expected)
        finally:
            os.close(parent_fd)

    def _remove_empty_owned_quarantine(
        self,
        parent_fd: int,
        quarantine_name: str,
        expected: tuple[int, int],
    ) -> None:
        try:
            current = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise OSError("worktree quarantine disappeared before final removal") from None
        if (current.st_dev, current.st_ino) != expected:
            raise OSError("worktree quarantine identity changed before final removal")
        os.rmdir(quarantine_name, dir_fd=parent_fd)
        self._worktree_quarantine_dir = None
        self._worktree_directory_removed = True

    def _quarantine_and_remove_owned_worktree_directory(self, path: str) -> None:
        expected = _directory_identity(self._worktree_dir_fd)
        quarantine_path = self._worktree_quarantine_dir
        if quarantine_path is None:
            quarantine_path = self._quarantine_owned_worktree_directory(path, expected)
            if quarantine_path == path and not os.path.exists(path):
                return
        self._remove_owned_worktree_quarantine(quarantine_path, expected)
