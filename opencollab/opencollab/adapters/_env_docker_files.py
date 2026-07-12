"""Docker-backed file reads, writes, temporary files, and removal."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
import uuid

from opencollab.adapters._env_config import (
    _DOCKER_CREATE_WRITE_AND_VERIFY,
    _DOCKER_REMOVE_OWNED_TEMP,
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
from opencollab.application.exception_notes import add_exception_note

logger = logging.getLogger(__name__)


class DockerFilesMixin:
    def _docker_temp_identities(self) -> dict[str, str]:
        identities = getattr(self, "_docker_temp_file_identities", None)
        if identities is None:
            identities = {}
            self._docker_temp_file_identities = identities
        return identities

    async def _run_verified_write(
        self,
        path: str,
        payload: bytes,
        *,
        script: str,
        operation: str,
    ) -> list[str]:
        write_timeout = _positive_finite_timeout(
            DOCKER_WRITE_TIMEOUT_SECONDS,
            name="DOCKER_WRITE_TIMEOUT_SECONDS",
        )
        self._ensure_active()
        await self._ensure_attached_container_bound()
        if not self._container_id:
            raise RuntimeError("Container not started. Call setup() first.")

        token = uuid.uuid4().hex
        exec_argv = self._build_exec_argv(
            script,
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
                raise OSError(
                    f"timed out {operation} could not be terminated inside attached container"
                ) from exc
            raise OSError(f"docker {operation} timed out for {path}") from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                detail = f"cancelled {operation} could not be terminated inside attached container"
                logger.error(detail)
                add_exception_note(exc, detail)
            raise
        except BaseException as original:
            if isinstance(original, _OwnedProcessNotQuiesced):
                self._aborted = True
            try:
                stopped = await self._cleanup_container_exec_or_revoke(token)
            except BaseException as cleanup_exc:
                stopped = False
                self._aborted = True
                add_exception_note(
                    original,
                    f"container {operation} cleanup failed: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                )
            if not stopped:
                detail = f"failed {operation} could not be terminated inside container"
                logger.error(detail)
                add_exception_note(original, detail)
            raise original

        if result.returncode == DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE:
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                self._aborted = True
                raise _OwnedProcessNotQuiesced(
                    f"container {operation} descendants did not quiesce",
                    cleanup_quiesced=False,
                )
            raise OSError(f"container {operation} descendants required forced cleanup")
        if (result.returncode or 0) != 0 or result.stdout_dropped_bytes or result.stderr_dropped_bytes:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise OSError(
                f"docker {operation} failed for {path} (exit {result.returncode}): {detail}"
            )
        verification = result.stdout.decode("utf-8", errors="strict").strip().splitlines()
        return verification[-1].split("\t") if verification else []

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
        payload = content.encode("utf-8")
        fields = await self._run_verified_write(
            path,
            payload,
            script=_DOCKER_WRITE_AND_VERIFY,
            operation="write",
        )
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
        payload = content.encode("utf-8")
        identity: str | None = None
        try:
            fields = await self._run_verified_write(
                path,
                payload,
                script=_DOCKER_CREATE_WRITE_AND_VERIFY,
                operation="temporary write",
            )
            if fields and re.fullmatch(r"[0-9]+:[0-9]+", fields[0]):
                identity = fields[0]
                self._docker_temp_identities()[path] = identity
            expected_digest = hashlib.sha256(payload).hexdigest()
            if fields != [identity, str(len(payload)), expected_digest]:
                raise OSError(
                    f"docker temporary write verification failed for {path}: "
                    f"expected a stable identity, {len(payload)} bytes and "
                    f"{expected_digest}, received {fields!r}"
                )
        except BaseException as original:
            if identity is not None:
                try:
                    await _await_owned_operation(self.remove_file(path))
                except BaseException as cleanup_exc:
                    add_exception_note(
                        original,
                        "container temporary file cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                    )
            raise
        return path

    async def remove_file(self, path: str) -> None:
        self._ensure_active()
        identities = self._docker_temp_identities()
        identity = identities.get(path)
        if identity is None:
            raise OSError(
                f"refusing to remove container temporary file without ownership proof: {path}"
            )
        command = (
            f"bash -c {shlex.quote(_DOCKER_REMOVE_OWNED_TEMP)} "
            f"opencollab-remove {shlex.quote(path)} {shlex.quote(identity)}"
        )
        result = await self.exec_cmd(
            command,
            timeout=DOCKER_WRITE_TIMEOUT_SECONDS,
        )
        if result.returncode == 76:
            raise OSError(
                f"refusing to remove replaced container temporary file: {path}"
            )
        if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
            raise OSError(f"failed to remove container temporary file: {path}")
        if identities.get(path) == identity:
            identities.pop(path, None)
