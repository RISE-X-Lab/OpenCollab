"""Docker execution environment with bounded command and ownership handling."""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import re
import shlex
import uuid
from collections.abc import Callable
from typing import NoReturn

from opencollab.adapters._env_base import (
    ENV_FILE_WRITE_LIMIT_BYTES,
    Environment,
    ExecResult,
    TextFileRange,
)
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
_WRITE_LOCKS: dict[str, asyncio.Lock] = {}

_EXEC_WRAPPER = r"""
pidfile=$1
shellflag=$2
command=$3
cleanup() { rm -f -- "$pidfile"; }
group_alive() {
    [ -n "$child" ] && kill -0 -- "-$child" 2>/dev/null
}
wait_for_group_exit() {
    attempts=0
    while group_alive; do
        attempts=$((attempts + 1))
        [ "$attempts" -ge 20 ] && return 1
        sleep 0.05
    done
    return 0
}
terminate() {
    if ! group_alive; then
        return 0
    fi
    kill -TERM -- "-$child" 2>/dev/null || true
    if wait_for_group_exit; then
        return 0
    fi
    kill -KILL -- "-$child" 2>/dev/null || true
    wait_for_group_exit
}
cancel_and_exit() {
    if terminate; then
        cleanup
        exit 143
    fi
    exit 125
}
child=
trap cancel_and_exit TERM INT HUP
set -m
bash "$shellflag" "$command" &
child=$!
printf '%s\n' "$child" > "$pidfile" || { terminate || true; cleanup; exit 125; }
wait "$child"
status=$?
if group_alive && ! terminate; then
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
group_alive() {
    kill -0 -- "-$child" 2>/dev/null
}
wait_for_group_exit() {
    attempts=0
    while group_alive; do
        attempts=$((attempts + 1))
        [ "$attempts" -ge 20 ] && return 1
        sleep 0.05
    done
    return 0
}
kill -TERM -- "-$child" 2>/dev/null || true
if ! wait_for_group_exit; then
    kill -KILL -- "-$child" 2>/dev/null || true
    if ! wait_for_group_exit; then
        exit 125
    fi
fi
rm -f -- "$pidfile" && exit 0
exit 125
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
        if (
            isinstance(timeout_returncode, bool)
            or not isinstance(timeout_returncode, int)
            or timeout_returncode == 0
        ):
            raise ValueError("timeout_returncode must be a non-zero integer")
        self._image = _validate_image(image)
        self.workspace = workspace
        self._attached = container_id is not None
        self._attached_reference = (
            _validate_container_reference(container_id) if container_id is not None else None
        )
        self._container_id = self._attached_reference if self._attached else None
        self._attached_bound = False
        self._attach_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._active_exec_lock = asyncio.Lock()
        self._active_execs: dict[str, asyncio.Task | None] = {}
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
            if (
                inspected.returncode != 0
                or inspected.stdout_dropped_bytes > 0
                or inspected.stderr_dropped_bytes > 0
            ):
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
        async with self._lifecycle_lock:
            return await self._setup_locked(mount_dir)

    async def _setup_locked(self, mount_dir: str | None) -> str:
        self._ensure_active()
        if self._attached:
            await self._bind_attached()
            assert self._container_id is not None
            return self._container_id
        if self._container_id is not None:
            return self._container_id
        host_mount: str | None = None
        if mount_dir is not None:
            host_mount = os.path.realpath(os.path.abspath(mount_dir))
            if not os.path.isdir(host_mount):
                raise NotADirectoryError(host_mount)
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
        if host_mount is not None:
            args.extend(("-v", f"{host_mount}:{self.workspace}"))
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
        self.host_workspace = host_mount
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
        async with self._active_exec_lock:
            self._ensure_active()
            self._active_execs[token] = asyncio.current_task()
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
        finally:
            async with self._active_exec_lock:
                self._active_execs.pop(token, None)
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

    async def read_text_range(
        self,
        path: str,
        *,
        offset: int,
        limit: int,
        max_chars: int,
    ) -> TextFileRange:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars < 1
        ):
            raise ValueError("offset, limit, and max_chars must be positive integers")
        final_line = offset + limit
        byte_cap = max_chars * 4 + limit + 1
        quoted_path = shlex.quote(path)
        command = (
            f"[ -f {quoted_path} ] && [ -r {quoted_path} ] || exit 66; "
            "command -v sed >/dev/null && command -v head >/dev/null || exit 127; "
            f"sed -n '{offset},{final_line}p;{final_line}q' < {quoted_path} "
            f"| head -c {byte_cap}"
        )
        result = await self.exec_cmd(command)
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        lines = result.stdout.splitlines()
        has_more = len(lines) > limit
        selected = lines[:limit]
        joined = "\n".join(selected)
        chars_truncated = len(joined) > max_chars or result.stdout_truncated
        if chars_truncated:
            selected = joined[:max_chars].split("\n")
            has_more = True
        return TextFileRange(
            selected,
            offset,
            None,
            has_more,
            chars_truncated=chars_truncated,
        )

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        await self._bind_attached()
        container_id = self._container_id
        if container_id is None:
            raise RuntimeError("Container not started. Call setup() first.")
        target = self._normalize_container_path(path)
        lock = _WRITE_LOCKS.setdefault(f"{container_id}\0{target}", asyncio.Lock())
        async with lock:
            self._ensure_active()
            await self._write_file_atomic(target, content)

    @staticmethod
    def _normalize_container_path(path: str) -> str:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("container file path must be non-empty text without NUL bytes")
        normalized = posixpath.normpath(path)
        if normalized in (".", "/") or posixpath.basename(normalized) in (".", ".."):
            raise ValueError("container file path must name a file")
        return normalized

    async def _write_file_atomic(self, target: str, content: str) -> None:
        payload = content.encode("utf-8")
        if len(payload) > ENV_FILE_WRITE_LIMIT_BYTES:
            raise OSError(
                f"docker file exceeds write limit of {ENV_FILE_WRITE_LIMIT_BYTES} bytes: {target}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        directory, filename = posixpath.split(target)
        temporary = posixpath.join(
            directory or ".",
            f".{filename}.opencollab-write-{uuid.uuid4().hex}.tmp",
        )
        command = (
            'target=$1; temporary=$2; expected_bytes=$3; expected_digest=$4; '
            'cleanup() { rm -f -- "$temporary"; }; trap cleanup EXIT HUP INT TERM; '
            'mkdir -p -- "$(dirname -- "$target")" && '
            '(umask 077; set -C; : > "$temporary") && '
            'cat > "$temporary" && '
            'bytes=$(wc -c < "$temporary") && '
            'digest=$(sha256sum -- "$temporary" 2>/dev/null | awk \'{print $1}\' || '
            'shasum -a 256 -- "$temporary" | awk \'{print $1}\') && '
            '[ "$bytes" = "$expected_bytes" ] && [ "$digest" = "$expected_digest" ] && '
            'mv -f -- "$temporary" "$target" && '
            'trap - EXIT HUP INT TERM && printf "%s\\t%s\\n" "$bytes" "$digest"'
        )
        wrapped = (
            f"bash -c {shlex.quote(command)} opencollab-write {shlex.quote(target)} "
            f"{shlex.quote(temporary)} {len(payload)} {digest}"
        )
        committed = False
        try:
            result = await self._exec(
                wrapped,
                timeout=DOCKER_WRITE_TIMEOUT_SECONDS,
                input_bytes=payload,
            )
            expected = f"{len(payload)}\t{digest}"
            if result.returncode != 0 or result.stdout.strip() != expected:
                raise OSError(f"docker write verification failed for {target}")
            committed = True
        finally:
            if not committed:
                await await_owned_operation(
                    self._discard_write_temporary(temporary),
                    propagate_cancellation=False,
                )

    async def _discard_write_temporary(self, temporary: str) -> None:
        container_id = self._container_id
        if container_id is None:
            return
        try:
            await self._docker(
                "exec",
                "--",
                container_id,
                "rm",
                "-f",
                "--",
                temporary,
            )
        except BaseException:
            pass

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

    async def _cleanup_attached_resources(self) -> None:
        container_id = self._container_id
        if container_id is None:
            if self._temporary_files:
                raise RuntimeError("attached Docker temporary files cannot be reached")
            return
        failures: list[BaseException] = []
        for path in tuple(self._temporary_files):
            try:
                removed = await self._docker(
                    "exec",
                    "--",
                    container_id,
                    "rm",
                    "-f",
                    "--",
                    path,
                )
            except BaseException as exc:
                failures.append(exc)
                continue
            if removed.returncode != 0:
                failures.append(
                    OSError(f"failed to remove container temporary file: {path}")
                )
                continue
            self._temporary_files.discard(path)
        if failures:
            raise OSError(
                "failed to remove one or more attached Docker temporary files"
            ) from failures[0]

    async def cleanup(self) -> None:
        async with self._lifecycle_lock:
            await self._abort_resources_locked()
            if self._attached:
                await await_owned_operation(
                    self._cleanup_attached_resources(),
                    propagate_cancellation=True,
                )
                return

    async def abort(self) -> None:
        async with self._lifecycle_lock:
            await self._abort_resources_locked()

    async def _abort_resources_locked(self) -> None:
        """Abort resources while the lifecycle lock is already held."""
        self.revoke()
        if not self._attached:
            await await_owned_operation(
                self._cleanup_resources(),
                propagate_cancellation=True,
            )
            return
        async with self._active_exec_lock:
            active = dict(self._active_execs)
        if not active:
            return
        cancelled = await asyncio.gather(
            *(self._cancel_inner(token) for token in active),
            return_exceptions=True,
        )
        current = asyncio.current_task()
        pending = {
            task
            for task in active.values()
            if task is not None and task is not current and not task.done()
        }
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=DOCKER_CONTROL_TIMEOUT_SECONDS,
            )
        failed_cancellations = any(
            result is not True and (task is None or not task.done())
            for result, task in zip(cancelled, active.values())
        )
        if failed_cancellations or pending:
            raise ProcessCleanupError(
                "attached container commands did not quiesce during abort"
            )

__all__ = ["DockerEnvironment"]
