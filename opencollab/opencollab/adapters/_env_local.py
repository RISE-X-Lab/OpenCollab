"""Local execution environment."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import threading

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import (
    LOCAL_FILE_READ_LIMIT_BYTES,
    LOCAL_FILE_WRITE_LIMIT_BYTES,
)
from opencollab.adapters._env_file_io import (
    _positive_file_size_limit,
    _positive_finite_timeout,
    _run_owned_blocking_io,
    _sync_create_temp_file,
    _sync_read_regular_file,
    _sync_unlink_file,
    _sync_write_regular_file,
)
from opencollab.adapters._env_process import (
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
)


class LocalEnvironment(Environment):
    """Direct OS execution — for interactive CLI use."""

    local_filesystem = True

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        workspace_stat = os.stat(self.workspace, follow_symlinks=False)
        if not stat.S_ISDIR(workspace_stat.st_mode):
            raise NotADirectoryError(self.workspace)
        self._workspace_identity = (workspace_stat.st_dev, workspace_stat.st_ino)
        self._temp_file_identities: dict[str, tuple[int, int]] = {}
        self._temp_identity_lock = threading.Lock()

    def _full_local_path(self, path: str) -> str:
        if not isinstance(path, str) or "\0" in path:
            raise ValueError("local file path must be text without NUL bytes")
        if os.path.isabs(path):
            return os.path.normpath(path)
        normalized = os.path.normpath(path)
        if normalized == ".." or normalized.startswith(f"..{os.sep}"):
            raise PermissionError(f"relative path escapes local workspace: {path}")
        return os.path.normpath(os.path.join(self.workspace, normalized))

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        timeout_seconds = _positive_finite_timeout(timeout, name="timeout")
        self._ensure_active()
        try:
            result = await _run_thread_owned_process(
                cmd,
                shell=True,
                cwd=self.workspace,
                timeout=timeout_seconds,
                timeout_name="timeout",
            )
            return ExecResult(
                returncode=result.returncode or 0,
                stdout=result.stdout.decode("utf-8", errors="replace"),
                stderr=result.stderr.decode("utf-8", errors="replace"),
                stdout_truncated=result.stdout_dropped_bytes > 0,
                stderr_truncated=result.stderr_dropped_bytes > 0,
                stdout_dropped_bytes=result.stdout_dropped_bytes,
                stderr_dropped_bytes=result.stderr_dropped_bytes,
            )
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            return ExecResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds:g}s",
            )
        except _OwnedProcessNotQuiesced:
            self._aborted = True
            raise
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            raise

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        full = self._full_local_path(path)
        limit = _positive_file_size_limit(
            LOCAL_FILE_READ_LIMIT_BYTES,
            name="LOCAL_FILE_READ_LIMIT_BYTES",
        )
        data = await _run_owned_blocking_io(_sync_read_regular_file, full, limit)
        assert isinstance(data, bytes)
        return data.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        full = self._full_local_path(path)
        limit = _positive_file_size_limit(
            LOCAL_FILE_WRITE_LIMIT_BYTES,
            name="LOCAL_FILE_WRITE_LIMIT_BYTES",
        )
        if len(content) > limit:
            raise OSError(f"local file exceeds write limit of {limit} bytes: {path}")
        payload = content.encode("utf-8")
        if len(payload) > limit:
            raise OSError(f"local file exceeds write limit of {limit} bytes: {path}")
        await _run_owned_blocking_io(_sync_write_regular_file, full, payload)

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if "/" in prefix or "/" in suffix or "\0" in prefix or "\0" in suffix:
            raise ValueError("temporary file prefix and suffix must be path components")
        limit = _positive_file_size_limit(
            LOCAL_FILE_WRITE_LIMIT_BYTES,
            name="LOCAL_FILE_WRITE_LIMIT_BYTES",
        )
        payload = content.encode("utf-8")
        if len(payload) > limit:
            raise OSError(f"local temporary file exceeds write limit of {limit} bytes")
        path, identity = _sync_create_temp_file(
            tempfile.gettempdir(),
            prefix,
            suffix,
            payload,
        )
        with self._temp_identity_lock:
            self._temp_file_identities[path] = identity
        return path

    async def remove_file(self, path: str) -> None:
        full = self._full_local_path(path)
        with self._temp_identity_lock:
            identity = self._temp_file_identities.get(full)
        await _run_owned_blocking_io(_sync_unlink_file, full, identity)
        if identity is not None:
            with self._temp_identity_lock:
                if self._temp_file_identities.get(full) == identity:
                    self._temp_file_identities.pop(full, None)
