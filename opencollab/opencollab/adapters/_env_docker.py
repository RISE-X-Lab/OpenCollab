"""Docker execution environment with bounded command and ownership handling."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import uuid
from collections.abc import Callable
from typing import NoReturn

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_process import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    ProcessCleanupError,
    run_process,
)
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note

DOCKER_OWNER_LABEL = "opencollab.owner"
DOCKER_SETUP_TIMEOUT_SECONDS = 120.0
DOCKER_CONTROL_TIMEOUT_SECONDS = 10.0
DOCKER_WRITE_TIMEOUT_SECONDS = 120.0

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_FULL_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_EXEC_WRAPPER = r"""
pidfile=$1
shellflag=$2
command=$3
cleanup() { rm -f -- "$pidfile"; }
terminate() {
    kill -TERM -- "-$child" 2>/dev/null || true
    sleep 0.1
    kill -KILL -- "-$child" 2>/dev/null || true
}
trap 'terminate; cleanup; exit 143' TERM INT HUP
set -m
bash "$shellflag" "$command" &
child=$!
printf '%s\n' "$child" > "$pidfile" || { terminate; cleanup; exit 125; }
wait "$child"
status=$?
if kill -0 -- "-$child" 2>/dev/null; then
    terminate
    cleanup
    exit 125
fi
cleanup
exit "$status"
""".strip()

_EXEC_CANCEL = r"""
pidfile=$1
if ! read -r child < "$pidfile" 2>/dev/null; then
    exit 124
fi
case "$child" in ''|*[!0-9]*) exit 125 ;; esac
kill -TERM -- "-$child" 2>/dev/null || true
sleep 0.1
kill -KILL -- "-$child" 2>/dev/null || true
rm -f -- "$pidfile"
exit 0
""".strip()


def _validate_image(value: str) -> str:
    if not isinstance(value, str) or not _IMAGE_RE.fullmatch(value) or value.startswith("-"):
        raise ValueError("Docker image reference is unsafe or malformed")
    return value


def _validate_container_reference(value: str) -> str:
    if not isinstance(value, str) or value.startswith("-"):
        raise ValueError("Docker container reference is unsafe or ambiguous")
    if _FULL_ID_RE.fullmatch(value):
        return value.lower()
    if not _NAME_RE.fullmatch(value) or re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ValueError("Docker container reference is unsafe or ambiguous")
    return value


class DockerEnvironment(Environment):
    """Run commands in a new network-isolated or caller-owned container."""

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
    ) -> None:
        super().__init__()
        if container_id is not None and backing_environment is not None:
            raise ValueError("an attached Docker environment cannot own a backing environment")
        self._image = _validate_image(image)
        self.workspace = workspace
        self._attached = container_id is not None
        self._attached_reference = (
            _validate_container_reference(container_id) if container_id is not None else None
        )
        self._container_id = self._attached_reference if self._attached else None
        self._attached_bound = False
        self._attach_lock = asyncio.Lock()
        self._container_name: str | None = None
        self._owner_token = None if self._attached else uuid.uuid4().hex
        self._exec_workdir = exec_workdir
        self._command_prefix = command_prefix
        self._timeout_returncode = timeout_returncode
        self._backing_environment = backing_environment
        self.source_workspace = getattr(backing_environment, "source_workspace", None)
        self.host_workspace = None
        self._temporary_files: set[str] = set()

    async def _docker(
        self,
        *args: str,
        timeout: float = DOCKER_CONTROL_TIMEOUT_SECONDS,
        input_bytes: bytes | None = None,
    ):
        return await run_process(
            ("docker", *args),
            shell=False,
            timeout=timeout,
            input_bytes=input_bytes,
            output_limit=PROCESS_OUTPUT_CAPTURE_BYTES,
        )

    async def _bind_attached(self) -> None:
        if not self._attached or self._attached_bound:
            return
        async with self._attach_lock:
            if self._attached_bound:
                return
            reference = self._attached_reference
            assert reference is not None
            inspected = await self._docker(
                "inspect",
                "--type",
                "container",
                "--format",
                '{{.Id}}{{printf "\\t"}}{{.Name}}{{printf "\\t"}}{{.State.Running}}',
                "--",
                reference,
            )
            if inspected.returncode != 0 or inspected.stdout_dropped_bytes > 0 or inspected.stderr_dropped_bytes > 0:
                raise RuntimeError("Could not inspect attached Docker container")
            fields = inspected.stdout.decode("utf-8", errors="strict").strip().split("\t")
            if len(fields) != 3 or not _FULL_ID_RE.fullmatch(fields[0]) or fields[2] != "true":
                raise RuntimeError("Attached Docker container identity was ambiguous or changed")
            if _FULL_ID_RE.fullmatch(reference):
                matches = fields[0].lower() == reference.lower()
            else:
                matches = fields[1] == f"/{reference}"
            if not matches:
                raise RuntimeError("Attached Docker container identity was ambiguous or changed")
            self._container_id = fields[0].lower()
            self._attached_bound = True

    async def setup(self, mount_dir: str | None = None) -> str:
        self._ensure_active()
        if self._attached:
            await self._bind_attached()
            assert self._container_id is not None
            return self._container_id
        if self._container_id is not None:
            return self._container_id
        self._container_name = f"opencollab-{uuid.uuid4().hex[:16]}"
        args = [
            "run",
            "-d",
            "--rm",
            "--network",
            "none",
            "--name",
            self._container_name,
            "--label",
            f"{DOCKER_OWNER_LABEL}={self._owner_token}",
        ]
        if mount_dir:
            args.extend(("-v", f"{os.path.abspath(mount_dir)}:{self.workspace}"))
        args.extend(("-w", self.workspace, self._image, "sleep", "infinity"))
        try:
            result = await self._docker(*args, timeout=DOCKER_SETUP_TIMEOUT_SECONDS)
        except BaseException as exc:
            await self._discard_failed_setup(exc)
        candidate = result.stdout.decode("ascii", errors="ignore").strip()
        if result.returncode != 0 or not _FULL_ID_RE.fullmatch(candidate):
            await self._discard_failed_setup(
                RuntimeError(
                    "Failed to start container: "
                    + result.stderr.decode("utf-8", errors="replace").strip()
                )
            )
        self._container_id = candidate.lower()
        return self._container_id

    async def _discard_failed_setup(self, failure: BaseException) -> NoReturn:
        try:
            removed = await await_owned_operation(
                self._remove_container_if_owned(),
                propagate_cancellation=not isinstance(failure, asyncio.CancelledError),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as cleanup_error:
            add_exception_note(
                failure,
                "Docker setup cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}",
            )
            raise failure from cleanup_error
        if not removed:
            cleanup_error = ProcessCleanupError(
                "Docker setup failed and owned container removal was not proven"
            )
            add_exception_note(
                cleanup_error,
                f"Original setup failure: {type(failure).__name__}: {failure}",
            )
            raise cleanup_error from failure
        raise failure

    async def _remove_container_if_owned(self) -> bool:
        if self._attached:
            return True
        container_id = self._container_id
        if container_id is None and self._container_name is not None:
            inspected = await self._docker(
                "inspect",
                "--type",
                "container",
                "--format",
                '{{.Id}}{{printf "\\t"}}{{index .Config.Labels "' + DOCKER_OWNER_LABEL + '"}}',
                "--",
                self._container_name,
            )
            if inspected.returncode != 0:
                return False
            fields = inspected.stdout.decode("utf-8", errors="replace").strip().split("\t")
            if len(fields) != 2 or fields[1] != self._owner_token or not _FULL_ID_RE.fullmatch(fields[0]):
                return False
            container_id = fields[0].lower()
        if container_id is None:
            return True
        removed = await self._docker("rm", "-f", "--", container_id)
        if removed.returncode != 0:
            return False
        self._container_id = None
        self._container_name = None
        return True

    def _wrap_command(self, cmd: str) -> str:
        if self._command_prefix is None:
            return cmd
        if callable(self._command_prefix):
            return self._command_prefix(cmd)
        return f"{self._command_prefix}\n{cmd}"

    def _exec_argv(self, cmd: str, token: str, *, interactive: bool = False) -> tuple[str, ...]:
        assert self._container_id is not None
        pidfile = f"/tmp/.opencollab-exec-{token}.pid"
        args = ["exec"]
        if interactive:
            args.append("-i")
        if self._exec_workdir:
            args.extend(("-w", self._exec_workdir))
        shell_flag = "-lc" if self._command_prefix is not None else "-c"
        args.extend(
            (
                "--",
                self._container_id,
                "bash",
                "-c",
                _EXEC_WRAPPER,
                "opencollab-exec",
                pidfile,
                shell_flag,
                self._wrap_command(cmd),
            )
        )
        return tuple(args)

    async def _cancel_inner(self, token: str) -> bool:
        if self._container_id is None:
            return False
        pidfile = f"/tmp/.opencollab-exec-{token}.pid"
        try:
            result = await self._docker(
                "exec",
                "--",
                self._container_id,
                "bash",
                "-c",
                _EXEC_CANCEL,
                "opencollab-cancel",
                pidfile,
            )
        except BaseException:
            return False
        return result.returncode == 0

    async def _recover_inner(self, token: str) -> bool:
        if await self._cancel_inner(token):
            return True
        if self._attached:
            self.revoke()
            return False
        removed = await self._remove_container_if_owned()
        if not removed:
            self.revoke()
        return removed

    async def _exec(
        self,
        cmd: str,
        *,
        timeout: float,
        input_bytes: bytes | None = None,
    ) -> ExecResult:
        self._ensure_active()
        await self._bind_attached()
        if self._container_id is None:
            raise RuntimeError("Container not started. Call setup() first.")
        token = uuid.uuid4().hex
        try:
            result = await self._docker(
                *self._exec_argv(cmd, token, interactive=input_bytes is not None),
                timeout=timeout,
                input_bytes=input_bytes,
            )
        except asyncio.TimeoutError:
            if not await await_owned_operation(
                self._recover_inner(token),
                propagate_cancellation=True,
            ):
                raise ProcessCleanupError("timed out container command did not quiesce")
            return ExecResult(
                self._timeout_returncode,
                "",
                f"Command timed out after {timeout:g}s",
            )
        except asyncio.CancelledError as exc:
            if not await await_owned_operation(self._recover_inner(token)):
                add_exception_note(exc, "cancelled container command did not quiesce")
            raise
        return result.to_exec_result()

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        return await self._exec(cmd, timeout=timeout)

    async def read_file(self, path: str) -> str:
        result = await self.exec_cmd(f"cat -- {shlex.quote(path)}")
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        if result.stdout_truncated:
            raise OSError(f"docker read exceeded capture limit for {path}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        payload = content.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        command = (
            'target=$1; mkdir -p -- "$(dirname -- "$target")" && cat > "$target" && '
            'bytes=$(wc -c < "$target") && '
            'digest=$(sha256sum -- "$target" 2>/dev/null | awk \'{print $1}\' || '
            'shasum -a 256 -- "$target" | awk \'{print $1}\') && '
            'printf "%s\\t%s\\n" "$bytes" "$digest"'
        )
        wrapped = f"bash -c {shlex.quote(command)} opencollab-write {shlex.quote(path)}"
        result = await self._exec(wrapped, timeout=DOCKER_WRITE_TIMEOUT_SECONDS, input_bytes=payload)
        expected = f"{len(payload)}\t{digest}"
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise OSError(f"docker write verification failed for {path}")

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        if any(character in prefix + suffix for character in ("/", "\0")):
            raise ValueError("temporary file prefix and suffix must be path components")
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        await self.write_file(path, content)
        self._temporary_files.add(path)
        return path

    async def remove_file(self, path: str) -> None:
        if path not in self._temporary_files:
            raise OSError(f"refusing to remove unowned container temporary file: {path}")
        result = await self.exec_cmd(f"rm -f -- {shlex.quote(path)}")
        if result.returncode != 0:
            raise OSError(f"failed to remove container temporary file: {path}")
        self._temporary_files.discard(path)

    async def _cleanup_resources(self) -> None:
        failures: list[BaseException] = []
        if not await self._remove_container_if_owned():
            failures.append(RuntimeError("owned Docker container could not be removed"))
        if self._backing_environment is not None:
            try:
                await self._backing_environment.cleanup()
            except BaseException as exc:
                failures.append(exc)
            else:
                self._backing_environment = None
        if failures:
            raise RuntimeError("Docker cleanup failed") from failures[0]

    async def cleanup(self) -> None:
        if self._attached:
            return
        self.revoke()
        await await_owned_operation(
            self._cleanup_resources(),
            propagate_cancellation=True,
        )

__all__ = ["DockerEnvironment"]
