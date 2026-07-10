"""Docker-backed file reads, writes, temporary files, and removal."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shlex
import uuid

from opencollab.adapters._env_config import (
    _DOCKER_WRITE_AND_VERIFY,
    DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE,
    DOCKER_WRITE_TIMEOUT_SECONDS,
)
from opencollab.adapters._env_file_io import _positive_finite_timeout
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
)

logger = logging.getLogger(__name__)


class DockerFilesMixin:
    async def read_file(self, path: str) -> str:
        self._ensure_active()
        quoted_path = shlex.quote(path)
        result = await self.exec_cmd(f"cat -- {quoted_path}")
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        if result.stdout_truncated:
            raise OSError(f"docker read exceeded capture limit for {path}; dropped {result.stdout_dropped_bytes} bytes")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        write_timeout = _positive_finite_timeout(
            DOCKER_WRITE_TIMEOUT_SECONDS,
            name="DOCKER_WRITE_TIMEOUT_SECONDS",
        )
        self._ensure_active()
        await self._ensure_attached_container_bound()
        if not self._container_id:
            raise RuntimeError("Container not started. Call setup() first.")

        token = uuid.uuid4().hex
        payload = content.encode("utf-8")
        exec_argv = self._build_exec_argv(
            _DOCKER_WRITE_AND_VERIFY,
            token,
            interactive=True,
            apply_prefix=False,
            extra_args=("opencollab-write", path),
        )

        try:
            result = await _run_thread_owned_process(
                exec_argv,
                shell=False,
                cwd=None,
                timeout=write_timeout,
                timeout_name="DOCKER_WRITE_TIMEOUT_SECONDS",
                input_data=payload,
            )
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                raise OSError("timed out write could not be terminated inside attached container") from exc
            raise OSError(f"docker write timed out for {path}") from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                detail = "cancelled write could not be terminated inside attached container"
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
                    add_note(f"container write cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
            if not stopped:
                detail = "failed write could not be terminated inside container"
                logger.error(detail)
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise original

        if result.returncode == DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE:
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                self._aborted = True
                raise _OwnedProcessNotQuiesced(
                    "container write descendants did not quiesce",
                    cleanup_quiesced=False,
                )
            raise OSError("container write descendants required forced cleanup")
        if (result.returncode or 0) != 0 or result.stdout_dropped_bytes or result.stderr_dropped_bytes:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise OSError(f"docker write failed for {path} (exit {result.returncode}): {detail}")
        verification = result.stdout.decode("utf-8", errors="strict").strip().splitlines()
        fields = verification[-1].split("\t") if verification else []
        expected_digest = hashlib.sha256(payload).hexdigest()
        if fields != [str(len(payload)), expected_digest]:
            raise OSError(
                f"docker write verification failed for {path}: "
                f"expected {len(payload)} bytes and {expected_digest}, "
                f"received {fields!r}"
            )

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        if "/" in prefix or "/" in suffix or "\0" in prefix or "\0" in suffix:
            raise ValueError("temporary file prefix and suffix must be path components")
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        try:
            created = await self.exec_cmd(
                "umask 077; set -o noclobber; : > " + shlex.quote(path),
                timeout=DOCKER_WRITE_TIMEOUT_SECONDS,
            )
            if created.returncode != 0 or created.stdout_truncated or created.stderr_truncated:
                raise OSError("failed to create exclusive container temporary file")
            await self.write_file(path, content)
        except BaseException as original:
            try:
                await _await_owned_operation(self.remove_file(path))
            except BaseException as cleanup_exc:
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(f"container temporary file cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
            raise
        return path

    async def remove_file(self, path: str) -> None:
        result = await self.exec_cmd(
            f"rm -f -- {shlex.quote(path)}",
            timeout=DOCKER_WRITE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
            raise OSError(f"failed to remove container temporary file: {path}")
