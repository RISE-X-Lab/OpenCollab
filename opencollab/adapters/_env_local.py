"""Local execution environment."""

from __future__ import annotations

import asyncio
import os
import re
import uuid

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_process import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    ProcessCleanupError,
    ProcessRegistry,
    run_process,
)
from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    read_regular_bytes,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)

LOCAL_FILE_READ_LIMIT_BYTES = 4 * 1024 * 1024
LOCAL_FILE_WRITE_LIMIT_BYTES = 4 * 1024 * 1024
_TEMP_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class LocalEnvironment(Environment):
    """Execute commands and bounded file operations in one host workspace."""

    local_filesystem = True
    process_isolated = False

    def __init__(self, workspace: str = ".") -> None:
        super().__init__()
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        if not os.path.isdir(self.workspace):
            raise NotADirectoryError(self.workspace)
        self.host_workspace = self.workspace
        self.source_workspace = self.workspace
        self._processes = ProcessRegistry()
        self._temporary_files: set[str] = set()

    def _full_path(self, path: str) -> str:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("local file path must be non-empty text without NUL bytes")
        if os.path.isabs(path):
            return os.path.normpath(path)
        normalized = os.path.normpath(path)
        if normalized == ".." or normalized.startswith(f"..{os.sep}"):
            raise PermissionError(f"relative path escapes local workspace: {path}")
        return os.path.join(self.workspace, normalized)

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure_active()
        try:
            result = await run_process(
                cmd,
                shell=True,
                cwd=self.workspace,
                timeout=timeout,
                registry=self._processes,
                output_limit=PROCESS_OUTPUT_CAPTURE_BYTES,
            )
        except asyncio.TimeoutError:
            return ExecResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout:g}s",
            )
        except ProcessCleanupError:
            self.revoke()
            raise
        return result.to_exec_result()

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        payload = read_regular_bytes(self._full_path(path), max_bytes=LOCAL_FILE_READ_LIMIT_BYTES)
        return payload.decode("utf-8")

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        payload = content.encode("utf-8")
        if len(payload) > LOCAL_FILE_WRITE_LIMIT_BYTES:
            raise OSError(
                f"local file exceeds write limit of {LOCAL_FILE_WRITE_LIMIT_BYTES} bytes: {path}"
            )
        write_regular_bytes_atomic(
            self._full_path(path),
            payload,
            max_bytes=LOCAL_FILE_WRITE_LIMIT_BYTES,
        )

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if not _TEMP_COMPONENT_RE.fullmatch(prefix) or not _TEMP_COMPONENT_RE.fullmatch(suffix):
            raise ValueError("temporary file prefix and suffix must be simple path components")
        payload = content.encode("utf-8")
        if len(payload) > LOCAL_FILE_WRITE_LIMIT_BYTES:
            raise OSError("temporary file exceeds local write limit")
        path = os.path.join(self.workspace, f"{prefix}{uuid.uuid4().hex}{suffix}")
        create_regular_bytes_atomic(
            path,
            payload,
            max_bytes=LOCAL_FILE_WRITE_LIMIT_BYTES,
        )
        self._temporary_files.add(path)
        return path

    async def remove_file(self, path: str) -> None:
        target = self._full_path(path)
        if target not in self._temporary_files:
            raise OSError(f"refusing to remove unowned local temporary file: {path}")
        unlink_regular_file_durable(target)
        self._temporary_files.discard(target)

    async def cleanup(self) -> None:
        self.revoke()
        await self._processes.abort()
        failures: list[OSError] = []
        for path in tuple(self._temporary_files):
            try:
                unlink_regular_file_durable(path)
            except OSError as exc:
                failures.append(exc)
            else:
                self._temporary_files.discard(path)
        if failures:
            raise OSError("failed to remove one or more environment temporary files") from failures[0]

__all__ = ["LocalEnvironment"]
