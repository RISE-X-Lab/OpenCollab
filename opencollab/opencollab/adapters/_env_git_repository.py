"""Descriptor-owned Git repository operations."""

from __future__ import annotations

import os
import stat
import threading

from opencollab.adapters._env_process import _sync_run_cleanup_command
from opencollab.application.exception_notes import add_exception_note


def trusted_git_command(*args: str) -> list[str]:
    return ["git", *args]


class _PinnedGitRepository:
    """Keep Git operations bound to one repository directory inode."""

    def __init__(self, path: str):
        self._identity: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._active_operations = 0
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            current = os.stat(path, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
            ):
                raise OSError("source repository changed while recording ownership")
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._identity = identity

    @property
    def fd(self) -> int:
        return self._fd

    def _verify(self, fd: int) -> None:
        if self._identity is None:
            raise RuntimeError("source repository identity is unavailable")
        value = os.fstat(fd)
        if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != self._identity:
            raise OSError("source repository descriptor identity changed")

    def acquire(self) -> int:
        with self._lock:
            if self._fd < 0:
                raise RuntimeError("source repository handle has been closed")
            self._verify(self._fd)
            result = os.dup(self._fd)
            try:
                self._verify(result)
            except BaseException:
                os.close(result)
                raise
            self._active_operations += 1
            return result

    def finish(self, operation_fd: int, original: BaseException | None = None) -> None:
        errors: list[BaseException] = []
        try:
            self._verify(operation_fd)
        except BaseException as exc:
            errors.append(exc)
        try:
            os.close(operation_fd)
        except BaseException as exc:
            errors.append(exc)
        with self._lock:
            self._active_operations -= 1
            try:
                if self._fd < 0:
                    raise RuntimeError("source repository handle closed during Git operation")
                self._verify(self._fd)
            except BaseException as exc:
                errors.append(exc)
        if original is not None:
            for error in errors:
                add_exception_note(original, f"source repository verification failed: {error}")
        elif errors:
            raise errors[0]

    def run_cleanup(self, *args: str, timeout: object, timeout_name: str):
        operation_fd = self.acquire()
        try:
            result = _sync_run_cleanup_command(
                trusted_git_command(*args), cwd=None, cwd_fd=operation_fd, timeout=timeout, timeout_name=timeout_name
            )
        except BaseException as original:
            self.finish(operation_fd, original)
            raise
        self.finish(operation_fd)
        return result

    def release(self) -> None:
        with self._lock:
            if self._fd < 0:
                return
            if self._active_operations:
                raise OSError("source repository still has active Git operations")
            self._verify(self._fd)
            os.close(self._fd)
            self._fd = -1


__all__ = ["_PinnedGitRepository", "trusted_git_command"]
