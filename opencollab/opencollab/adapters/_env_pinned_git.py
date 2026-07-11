"""Descriptor-pinned Git repository process ownership."""

from __future__ import annotations

import os
import re
import stat
import threading

from opencollab.adapters._env_process import _sync_run_cleanup_command
from opencollab.application.exception_notes import add_exception_note


def trusted_git_command(*args: str) -> list[str]:
    """Return Git with repository-private and ambient configuration disabled."""
    return [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "HOME=/",
        "XDG_CONFIG_HOME=/nonexistent",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_CONFIG_SYSTEM=/dev/null",
        "GIT_ATTR_NOSYSTEM=1",
        "git",
        *args,
    ]


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
        identity = self._identity
        if identity is None:
            raise RuntimeError("source repository identity is unavailable")
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
            raise OSError("source repository descriptor identity changed")

    def acquire(self) -> int:
        with self._lock:
            if self._fd < 0:
                raise RuntimeError("source repository handle has been closed")
            self._verify(self._fd)
            operation_fd = os.dup(self._fd)
            try:
                self._verify(operation_fd)
            except BaseException:
                os.close(operation_fd)
                raise
            self._active_operations += 1
            return operation_fd

    def finish(
        self,
        operation_fd: int,
        original: BaseException | None = None,
    ) -> None:
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
                add_exception_note(
                    original,
                    "source repository verification failed after Git operation: "
                    f"{type(error).__name__}: {error}",
                )
            return
        if errors:
            first = errors[0]
            for error in errors[1:]:
                add_exception_note(
                    first,
                    "additional source repository verification failure: "
                    f"{type(error).__name__}: {error}",
                )
            raise first

    def run_cleanup(
        self,
        *args: str,
        timeout: object,
        timeout_name: str,
    ) -> tuple[int, bytes, bytes, bool]:
        operation_fd = self.acquire()
        try:
            result = _sync_run_cleanup_command(
                trusted_git_command(*args),
                cwd=None,
                cwd_fd=operation_fd,
                timeout=timeout,
                timeout_name=timeout_name,
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


_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def capture_patch_source(path: str) -> tuple[str, str] | None:
    """Pin the current commit and object directory before task execution."""
    try:
        os.lstat(os.path.join(path, ".git"))
    except FileNotFoundError:
        return None
    repository = _PinnedGitRepository(path)
    try:
        root_result = repository.run_cleanup(
            "rev-parse",
            "--show-toplevel",
            timeout=30.0,
            timeout_name="Git patch source discovery",
        )
        if root_result[0] != 0 or not root_result[3]:
            return None
        root = root_result[1].decode("utf-8", errors="strict").strip()
        if os.path.realpath(root) != os.path.realpath(path):
            return None
        base_result = repository.run_cleanup(
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            timeout=30.0,
            timeout_name="Git patch base discovery",
        )
        if base_result[0] != 0 or not base_result[3]:
            return None
        base_oid = base_result[1].decode("ascii", errors="strict").strip().lower()
        if _OID_RE.fullmatch(base_oid) is None:
            raise OSError("Git patch base is not an exact object id")
        object_result = repository.run_cleanup(
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
            timeout=30.0,
            timeout_name="Git object directory discovery",
        )
        if object_result[0] != 0 or not object_result[3]:
            raise OSError("Git patch object directory is unavailable")
        object_directory = os.path.realpath(
            object_result[1].decode("utf-8", errors="strict").strip()
        )
        object_info = os.stat(object_directory, follow_symlinks=False)
        if not os.path.isabs(object_directory) or not stat.S_ISDIR(object_info.st_mode):
            raise OSError("Git patch object directory is invalid")
        return base_oid, object_directory
    finally:
        repository.release()


__all__ = ["_PinnedGitRepository", "capture_patch_source", "trusted_git_command"]
