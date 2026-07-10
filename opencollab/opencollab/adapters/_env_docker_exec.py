"""Docker exec command construction, cancellation, and quiescence."""

from __future__ import annotations

import asyncio
import logging
import uuid

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters._env_config import (
    _DOCKER_EXEC_CANCEL,
    _DOCKER_EXEC_WRAPPER,
    DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS,
    DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE,
)
from opencollab.adapters._env_file_io import _positive_finite_timeout
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
)

logger = logging.getLogger(__name__)


class DockerExecMixin:
    def _wrap_command(self, cmd: str) -> str:
        prefix = self._command_prefix
        if prefix is None:
            return cmd
        if callable(prefix):
            return prefix(cmd)
        return f"{prefix}\n{cmd}"

    @staticmethod
    def _exec_state_paths(token: str) -> tuple[str, str]:
        stem = f"/tmp/.opencollab-exec-{token}"
        return f"{stem}.pid", f"{stem}.cancel"

    def _build_exec_argv(
        self,
        command: str,
        token: str,
        *,
        interactive: bool = False,
        apply_prefix: bool = True,
        extra_args: tuple[str, ...] = (),
    ) -> list[str]:
        if not self._container_id:
            raise RuntimeError("Container not started. Call setup() first.")
        pidfile, cancelfile = self._exec_state_paths(token)
        argv = ["docker", "exec"]
        if interactive:
            argv.append("-i")
        if self._exec_workdir:
            argv += ["-w", self._exec_workdir]
        shell_flag = "-lc" if apply_prefix and self._command_prefix is not None else "-c"
        wrapped = self._wrap_command(command) if apply_prefix else command
        argv += [
            "--",
            self._container_id,
            "bash",
            "-c",
            _DOCKER_EXEC_WRAPPER,
            "opencollab-exec",
            pidfile,
            cancelfile,
            shell_flag,
            wrapped,
            *extra_args,
        ]
        return argv

    async def _terminate_container_exec(self, token: str) -> bool:
        """Cancel the process group created inside the target container."""
        if not self._container_id:
            return False
        timeout = _positive_finite_timeout(
            DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS,
            name="DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS",
        )
        pidfile, cancelfile = self._exec_state_paths(token)
        try:
            result = await _run_thread_owned_process(
                [
                    "docker",
                    "exec",
                    "--",
                    self._container_id,
                    "bash",
                    "-c",
                    _DOCKER_EXEC_CANCEL,
                    "opencollab-cancel",
                    pidfile,
                    cancelfile,
                ],
                shell=False,
                cwd=None,
                timeout=timeout,
                timeout_name="DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS",
            )
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            logger.warning("docker exec cancellation command timed out for %s", token)
            return False
        except Exception as exc:
            if isinstance(exc, _OwnedProcessNotQuiesced):
                self._aborted = True
            logger.warning("docker exec cancellation failed for %s: %s", token, exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "docker exec cancellation exited %s for %s: %s",
                result.returncode,
                token,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def _ensure_container_exec_stopped(self, token: str) -> bool:
        if await self._terminate_container_exec(token):
            return True
        if self._attached:
            return False
        container_ref = self._container_name or self._container_id
        if not container_ref:
            return False
        return await self._force_remove_container(container_ref)

    async def _cleanup_container_exec(
        self,
        token: str,
    ) -> bool:
        return await self._ensure_container_exec_stopped(token)

    async def _cleanup_container_exec_or_revoke(self, token: str) -> bool:
        """Attempt inner cleanup and revoke the adapter on every unknown result."""
        try:
            stopped = await _await_owned_operation(self._cleanup_container_exec(token))
        except BaseException:
            self._aborted = True
            raise
        if stopped is not True:
            self._aborted = True
            return False
        return True

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        timeout_seconds = _positive_finite_timeout(timeout, name="timeout")
        self._ensure_active()
        await self._ensure_attached_container_bound()
        if not self._container_id:
            raise RuntimeError("Container not started. Call setup() first.")

        token = uuid.uuid4().hex
        exec_argv = self._build_exec_argv(cmd, token)
        try:
            result = await _run_thread_owned_process(
                exec_argv,
                shell=False,
                cwd=None,
                timeout=timeout_seconds,
                timeout_name="timeout",
            )
            if result.returncode == DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE:
                stopped = await self._cleanup_container_exec_or_revoke(token)
                if not stopped:
                    self._aborted = True
                    raise _OwnedProcessNotQuiesced(
                        "container command descendants did not quiesce",
                        cleanup_quiesced=False,
                    )
                raise OSError("container command descendants required forced cleanup")
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
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                raise OSError("timed out command could not be terminated inside attached container")
            return ExecResult(
                returncode=self._timeout_returncode,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds:g}s",
            )
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                detail = "cancelled command could not be terminated inside attached container"
                logger.error(detail)
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise
        except BaseException as original:
            if isinstance(original, _OwnedProcessNotQuiesced):
                self._aborted = True
            try:
                stopped = await self._cleanup_container_exec_or_revoke(token)
            except BaseException as cleanup_exc:
                stopped = False
                self._aborted = True
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(f"container command cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
            if not stopped:
                detail = "failed command could not be terminated inside container"
                logger.error(detail)
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise original
