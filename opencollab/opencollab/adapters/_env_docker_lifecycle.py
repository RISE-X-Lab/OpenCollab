"""Docker container binding, setup, ownership, and removal."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from collections.abc import Callable

from opencollab.adapters._env_base import Environment
from opencollab.adapters._env_config import (
    _DOCKER_ATTACH_INSPECT_FORMAT,
    _DOCKER_INSPECT_FORMAT,
    _DOCKER_MISSING_RE,
    DOCKER_COMPENSATION_TIMEOUT_SECONDS,
    DOCKER_OWNER_LABEL,
    DOCKER_SETUP_TIMEOUT_SECONDS,
    _validate_docker_container_reference,
    _validate_docker_image_reference,
)
from opencollab.adapters._env_file_io import _positive_finite_timeout
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
    _sync_run_cleanup_command,
)

logger = logging.getLogger(__name__)


class DockerLifecycleMixin:
    """Docker container sandbox for isolated agent execution.

    Two modes:
    - Start mode (default): ``setup()`` starts a fresh container, optionally
      mounting a local directory.
    - Attach mode: pass ``container_id`` to target an ALREADY-RUNNING container
      started outside this process. No ``setup()`` call is needed and
      ``cleanup()`` leaves the container alone.

    ``exec_workdir`` sets the ``docker exec -w`` working directory. ``command_prefix``
    wraps each command before execution (e.g. activating a conda env). When a prefix
    is supplied, commands run through a login shell (``bash -lc``) so the activation
    sticks. ``timeout_returncode`` is the ``returncode`` reported on timeout.

    Ref: environment abstraction design.
    """

    process_isolated = True

    def __init__(
        self,
        image: str = "python:3.11-slim",
        workspace: str = "/workspace",
        *,
        container_id: str | None = None,
        exec_workdir: str | None = None,
        command_prefix: Callable[[str], str] | str | None = None,
        timeout_returncode: int = -1,
        backing_environment: Environment | None = None,
    ):
        if container_id is not None and backing_environment is not None:
            raise ValueError("an attached Docker environment cannot own a backing environment")
        self._image = _validate_docker_image_reference(image)
        self.workspace = workspace
        attached_reference = _validate_docker_container_reference(container_id) if container_id is not None else None
        self._container_id = attached_reference
        self._attached_reference = attached_reference
        self._attached_reference_bound = False
        self._attach_binding_lock = asyncio.Lock()
        self._exec_workdir = exec_workdir
        self._command_prefix = command_prefix
        self._timeout_returncode = timeout_returncode
        self._attached = container_id is not None
        self._container_name: str | None = None
        self._owner_token: str | None = None if self._attached else uuid.uuid4().hex
        self.host_workspace = None
        self.source_workspace = getattr(
            backing_environment,
            "source_workspace",
            None,
        )
        self._backing_environment = backing_environment

    @staticmethod
    def _parse_attached_container_inspect(
        stdout: bytes,
        *,
        expected_reference: str,
    ) -> str:
        try:
            text = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return ""
        fields = text.split("\t")
        if len(fields) != 3:
            return ""
        container_id, actual_name, running = fields
        if re.fullmatch(r"[0-9a-fA-F]{64}", container_id) is None or running != "true":
            return ""
        if re.fullmatch(r"[0-9a-fA-F]{64}", expected_reference):
            if container_id.lower() != expected_reference.lower():
                return ""
        elif actual_name != f"/{expected_reference}":
            return ""
        return container_id.lower()

    async def _ensure_attached_container_bound(self) -> None:
        if not self._attached or self._attached_reference_bound:
            return
        async with self._attach_binding_lock:
            if self._attached_reference_bound:
                return
            reference = self._attached_reference
            if reference is None:
                raise RuntimeError("Attached container reference is unavailable")
            timeout = _positive_finite_timeout(
                DOCKER_COMPENSATION_TIMEOUT_SECONDS,
                name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
            )
            try:
                result = await _run_thread_owned_process(
                    [
                        "docker",
                        "inspect",
                        "--type",
                        "container",
                        "--format",
                        _DOCKER_ATTACH_INSPECT_FORMAT,
                        "--",
                        reference,
                    ],
                    shell=False,
                    cwd=None,
                    timeout=timeout,
                    timeout_name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
                )
            except (_OwnedProcessTimeout, _OwnedProcessNotQuiesced) as exc:
                if getattr(exc, "cleanup_quiesced", True) is False:
                    self._aborted = True
                raise RuntimeError("Could not bind attached Docker container to a full id") from exc
            if result.returncode != 0 or result.stdout_dropped_bytes or result.stderr_dropped_bytes:
                detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
                raise RuntimeError("Could not inspect attached Docker container: " + detail)
            full_id = self._parse_attached_container_inspect(
                result.stdout,
                expected_reference=reference,
            )
            if not full_id:
                raise RuntimeError("Attached Docker container identity was ambiguous or changed")
            self._container_id = full_id
            self._attached_reference_bound = True

    @staticmethod
    def _new_container_name() -> str:
        return f"opencollab-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _container_id_from_output(stdout: bytes) -> str:
        try:
            candidate = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return ""
        return candidate if re.fullmatch(r"[0-9a-fA-F]{64}", candidate) else ""

    @staticmethod
    def _parse_container_inspect(
        stdout: bytes,
        *,
        expected_name: str,
        expected_token: str,
    ) -> str:
        try:
            text = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return ""
        fields = text.split("\t")
        if len(fields) != 3:
            return ""
        container_id, actual_name, actual_token = fields
        if (
            re.fullmatch(r"[0-9a-fA-F]{64}", container_id) is None
            or actual_name != f"/{expected_name}"
            or actual_token != expected_token
        ):
            return ""
        return container_id

    def _sync_inspect_owned_container(
        self,
        reference: str,
        *,
        container_name: str,
    ) -> tuple[str, str]:
        token = self._owner_token
        if not token:
            return "unknown", ""
        try:
            returncode, stdout, stderr, quiesced = _sync_run_cleanup_command(
                [
                    "docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    _DOCKER_INSPECT_FORMAT,
                    "--",
                    reference,
                ],
                cwd=None,
                timeout=DOCKER_COMPENSATION_TIMEOUT_SECONDS,
                timeout_name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
            )
        except BaseException as exc:
            logger.error("docker ownership inspect failed for %s: %s", reference, exc)
            return "unknown", ""
        detail = stderr or stdout
        if returncode != 0:
            if quiesced and _DOCKER_MISSING_RE.search(detail):
                return "absent", ""
            return "unknown", ""
        if not quiesced:
            return "unknown", ""
        container_id = self._parse_container_inspect(
            stdout,
            expected_name=container_name,
            expected_token=token,
        )
        return ("owned", container_id) if container_id else ("foreign", "")

    def _sync_compensate_failed_setup(
        self,
        container_name: str,
        stdout: bytes,
    ) -> None:
        late_id = self._container_id_from_output(stdout)
        state, inspected_id = self._sync_inspect_owned_container(
            container_name,
            container_name=container_name,
        )
        if state == "absent":
            self._container_id = None
            self._container_name = None
            return
        if state != "owned" or (late_id and late_id != inspected_id):
            self._container_name = container_name
            raise OSError("docker setup compensation could not prove container ownership")
        self._container_id = inspected_id
        try:
            returncode, _out, stderr, quiesced = _sync_run_cleanup_command(
                ["docker", "rm", "-f", inspected_id],
                cwd=None,
                timeout=DOCKER_COMPENSATION_TIMEOUT_SECONDS,
                timeout_name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
            )
        except BaseException as exc:
            raise OSError("docker owned setup compensation failed") from exc
        if not quiesced or (returncode != 0 and _DOCKER_MISSING_RE.search(stderr) is None):
            raise OSError("docker owned setup compensation did not complete")
        for reference in (inspected_id, container_name):
            absence, _unused = self._sync_inspect_owned_container(
                reference,
                container_name=container_name,
            )
            if absence != "absent":
                raise OSError("docker setup compensation could not prove absence")
        self._container_id = None
        self._container_name = None

    async def setup(self, mount_dir: str | None = None) -> str:
        """Start a container. Optionally mount a local directory."""
        setup_timeout = _positive_finite_timeout(
            DOCKER_SETUP_TIMEOUT_SECONDS,
            name="DOCKER_SETUP_TIMEOUT_SECONDS",
        )
        self._ensure_active()
        self.host_workspace = os.path.abspath(mount_dir) if mount_dir else None
        container_name = self._new_container_name()
        if self._owner_token is None:
            self._owner_token = uuid.uuid4().hex
        # Keep the unique daemon reference from the moment setup begins. If run
        # or compensation fails, callers can retry cleanup by name even though
        # Docker never returned a container id.
        self._container_name = container_name
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--label",
            f"{DOCKER_OWNER_LABEL}={self._owner_token}",
        ]
        if mount_dir:
            cmd += ["-v", f"{os.path.abspath(mount_dir)}:{self.workspace}"]
        cmd += ["-w", self.workspace, "--", self._image, "sleep", "infinity"]

        try:
            result = await _run_thread_owned_process(
                cmd,
                shell=False,
                cwd=None,
                timeout=setup_timeout,
                timeout_name="DOCKER_SETUP_TIMEOUT_SECONDS",
                late_compensation=lambda process_result: self._sync_compensate_failed_setup(
                    container_name,
                    process_result.stdout,
                ),
            )
        except _OwnedProcessNotQuiesced:
            self._aborted = True
            raise
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            raise RuntimeError(
                f"Timed out starting Docker container after {setup_timeout:g}s; cleanup_quiesced={exc.cleanup_quiesced}"
            ) from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            raise
        if result.returncode != 0:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: "
                + result.stderr.decode(errors="replace")
                + (" (cleanup failed; container reference retained)" if not removed else "")
            )

        if result.stdout_dropped_bytes:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: id output was truncated"
                + (" (cleanup failed; container reference retained)" if not removed else "")
            )
        self._container_id = self._container_id_from_output(result.stdout)
        if not self._container_id:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: docker returned no container id"
                + (" (cleanup failed; container reference retained)" if not removed else "")
            )
        ownership, inspected_id = await self._inspect_owned_container(
            self._container_id,
            container_name=container_name,
        )
        if ownership != "owned" or inspected_id != self._container_id:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: Docker ownership proof did not match"
                + (" (cleanup failed; container reference retained)" if not removed else "")
            )
        self._container_name = container_name
        return self._container_id

    async def _inspect_owned_container(
        self,
        reference: str,
        *,
        container_name: str,
    ) -> tuple[str, str]:
        token = self._owner_token
        if not token:
            return "unknown", ""
        timeout = _positive_finite_timeout(
            DOCKER_COMPENSATION_TIMEOUT_SECONDS,
            name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
        )
        try:
            result = await _run_thread_owned_process(
                [
                    "docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    _DOCKER_INSPECT_FORMAT,
                    "--",
                    reference,
                ],
                shell=False,
                cwd=None,
                timeout=timeout,
                timeout_name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
            )
        except (_OwnedProcessTimeout, _OwnedProcessNotQuiesced) as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            return "unknown", ""
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown", ""
        detail = result.stderr or result.stdout
        if result.returncode != 0:
            if _DOCKER_MISSING_RE.search(detail):
                return "absent", ""
            return "unknown", ""
        container_id = self._parse_container_inspect(
            result.stdout,
            expected_name=container_name,
            expected_token=token,
        )
        return ("owned", container_id) if container_id else ("foreign", "")

    async def _force_remove_container(self, container_name: str) -> bool:
        """Remove only a reference proven to carry this adapter's owner label."""
        timeout = _positive_finite_timeout(
            DOCKER_COMPENSATION_TIMEOUT_SECONDS,
            name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
        )
        expected_name = self._container_name or container_name
        ownership, inspected_id = await self._inspect_owned_container(
            container_name,
            container_name=expected_name,
        )
        if ownership == "absent":
            self._container_id = None
            self._container_name = None
            return True
        if ownership != "owned":
            logger.warning(
                "refusing to remove Docker container without owner proof: %s",
                container_name,
            )
            return False
        if (
            self._container_id
            and re.fullmatch(r"[0-9a-fA-F]{64}", self._container_id)
            and self._container_id != inspected_id
        ):
            logger.warning("Docker container id changed before cleanup")
            return False
        try:
            result = await _run_thread_owned_process(
                ["docker", "rm", "-f", inspected_id],
                shell=False,
                cwd=None,
                timeout=timeout,
                timeout_name="DOCKER_COMPENSATION_TIMEOUT_SECONDS",
            )
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            logger.warning("docker rm -f timed out for %s", container_name)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, _OwnedProcessNotQuiesced):
                self._aborted = True
            logger.warning(
                "docker setup compensation failed for %s: %s",
                container_name,
                exc,
            )
            return False
        missing = _DOCKER_MISSING_RE.search(result.stderr) is not None
        if result.returncode != 0 and not missing:
            logger.warning(
                "docker rm -f exited %s for %s: %s",
                result.returncode,
                inspected_id,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        for reference in (inspected_id, expected_name):
            state, _unused = await self._inspect_owned_container(
                reference,
                container_name=expected_name,
            )
            if state != "absent":
                logger.warning("Docker cleanup could not prove reference absent: %s", reference)
                return False
        self._container_id = None
        self._container_name = None
        return True

    async def _remove_owned_container_or_raise(self, *, operation: str) -> None:
        container_ref = self._container_name or self._container_id
        if not container_ref:
            return
        if await _await_owned_operation(self._force_remove_container(container_ref)):
            return
        raise OSError(
            f"Docker container {operation} failed for {container_ref}; container reference retained for recovery"
        )
