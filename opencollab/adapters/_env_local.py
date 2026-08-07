"""Local execution environment."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable
from typing import TypeVar

from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES, Environment, ExecResult
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
from opencollab.application.async_timeout import await_owned_operation, consume_task_result

LOCAL_FILE_READ_LIMIT_BYTES = 4 * 1024 * 1024
LOCAL_FILE_WRITE_LIMIT_BYTES = ENV_FILE_WRITE_LIMIT_BYTES
_TEMP_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FILE_IO_CONCURRENCY = 4
T = TypeVar("T")


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
        self._file_io_semaphore = asyncio.Semaphore(_FILE_IO_CONCURRENCY)
        self._file_operations: set[asyncio.Task] = set()

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

    async def _execute_file_operation(
        self,
        operation: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> T:
        async with self._file_io_semaphore:
            return await asyncio.to_thread(operation, *args, **kwargs)

    async def _run_file_operation(
        self,
        operation: Callable[..., T],
        *args: object,
        require_active: bool = True,
        **kwargs: object,
    ) -> T:
        if require_active:
            self._ensure_active()
        owner = asyncio.create_task(
            self._execute_file_operation(operation, *args, **kwargs)
        )
        self._file_operations.add(owner)
        owner.add_done_callback(self._file_operations.discard)
        owner.add_done_callback(consume_task_result)
        return await asyncio.shield(owner)

    async def read_file(self, path: str) -> str:
        payload = await self._run_file_operation(
            read_regular_bytes,
            self._full_path(path),
            max_bytes=LOCAL_FILE_READ_LIMIT_BYTES,
        )
        return payload.decode("utf-8")

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        payload = content.encode("utf-8")
        if len(payload) > LOCAL_FILE_WRITE_LIMIT_BYTES:
            raise OSError(
                f"local file exceeds write limit of {LOCAL_FILE_WRITE_LIMIT_BYTES} bytes: {path}"
            )
        await self._run_file_operation(
            write_regular_bytes_atomic,
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
        self._temporary_files.add(path)
        await self._run_file_operation(
            create_regular_bytes_atomic,
            path,
            payload,
            max_bytes=LOCAL_FILE_WRITE_LIMIT_BYTES,
        )
        return path

    async def remove_file(self, path: str) -> None:
        self._ensure_active()
        target = self._full_path(path)
        if target not in self._temporary_files:
            raise OSError(f"refusing to remove unowned local temporary file: {path}")
        await self._run_file_operation(unlink_regular_file_durable, target)
        self._temporary_files.discard(target)

    async def _cleanup_files(self) -> None:
        while self._file_operations:
            pending = tuple(self._file_operations)
            await asyncio.gather(*pending, return_exceptions=True)
        failures: list[OSError] = []
        for path in tuple(self._temporary_files):
            try:
                await self._run_file_operation(
                    unlink_regular_file_durable,
                    path,
                    require_active=False,
                )
            except OSError as exc:
                failures.append(exc)
            else:
                self._temporary_files.discard(path)
        if failures:
            raise OSError("failed to remove one or more environment temporary files") from failures[0]

    async def cleanup(self) -> None:
        self.revoke()
        await self._processes.abort()
        await await_owned_operation(
            self._cleanup_files(),
            propagate_cancellation=True,
        )

__all__ = ["LocalEnvironment"]
