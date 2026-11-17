"""Docker execution environment with bounded command and ownership handling."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import os
import posixpath
import re
import shlex
import stat
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
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
    timed_out_result,
)
from opencollab.adapters._safe_file_common import _normalize_newlines, _split_lines
from opencollab.adapters.tools.base import _write_lock_paths
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.domain.docker import DockerOptions

DOCKER_OWNER_LABEL = "opencollab.owner"
DOCKER_SETUP_TIMEOUT_SECONDS = 120.0
# Daemon control calls can outlive process exit while storage/network cleanup
# completes, especially when many evaluation containers finish together.
DOCKER_CONTROL_TIMEOUT_SECONDS = 300.0
DOCKER_WRITE_TIMEOUT_SECONDS = 120.0


def _output_marker() -> str:
    return f"OCFILE{uuid.uuid4().hex}"


def _framed(marker: str, command: str) -> str:
    return f"printf '%s\\n' {shlex.quote(marker)}; {command}"


def _text_after_marker(stdout: str, marker: str) -> str:
    token = marker + "\n"
    index = stdout.find(token)
    if index < 0:
        raise OSError("docker file command output lost its framing marker")
    return stdout[index + len(token):]


def _within_directory(root: str, path: str) -> bool:
    root_norm = posixpath.normpath(root)
    path_norm = posixpath.normpath(path)
    if not posixpath.isabs(root_norm) or not posixpath.isabs(path_norm):
        return False
    if path_norm == root_norm:
        return True
    return path_norm.startswith(root_norm.rstrip("/") + "/")


def _write_receipt(stdout: str, expected: str, *, observe_change: bool) -> str | None:
    found = None
    for line in stdout.splitlines():
        if observe_change:
            fields = line.split("\t")
            if (
                len(fields) == 3
                and fields[2] in {"equal", "different", "unknown"}
                and "\t".join(fields[:2]) == expected
            ):
                found = line
        elif line == expected:
            found = line
    return found

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_FULL_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
@dataclass
class _WriteLockEntry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0

_WRITE_LOCKS: dict[str, _WriteLockEntry] = {}
_WRITE_LOCK_OWNERS: dict[str, asyncio.Task] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

# Git commits need an author in benchmark images. Pass an anonymous identity
# per exec so handoff commits succeed without changing the repository config
# that the evaluation harness later verifies. ``worktree_changes`` attributes
# the commits to agents.
_GIT_IDENTITY_ENV: tuple[tuple[str, str], ...] = (
    ("GIT_AUTHOR_NAME", "OpenCollab Agent"),
    ("GIT_AUTHOR_EMAIL", "agent@opencollab.invalid"),
    ("GIT_COMMITTER_NAME", "OpenCollab Agent"),
    ("GIT_COMMITTER_EMAIL", "agent@opencollab.invalid"),
)

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
    """Run commands in a new network-isolated or caller-owned container.

    A recoverable command timeout retains the running container. If cancellation
    cannot quiesce the command, owned containers are stopped and revoked while
    preserving their filesystem for caller recovery (for example, docker cp).
    abort() also preserves the container and backing workspace. The owner must
    call cleanup() after collecting artifacts to remove these resources.
    """

    process_isolated = True

    def __init__(
        self,
        image: str | None = None,
        workspace: str = "/workspace",
        *,
        container_id: str | None = None,
        exec_workdir: str | None = None,
        command_prefix: Callable[[str], str] | str | None = None,
        timeout_returncode: int = -1,
        backing_environment: Environment | None = None,
        options: DockerOptions | None = None,
        init_process: bool = False,
    ) -> None:
        super().__init__()
        self._options = options or DockerOptions()
        if container_id is not None and options is not None:
            raise ValueError("Docker options apply only to newly owned containers")
        if container_id is not None and backing_environment is not None:
            raise ValueError("an attached Docker environment cannot own a backing environment")
        if (
            isinstance(timeout_returncode, bool)
            or not isinstance(timeout_returncode, int)
            or timeout_returncode == 0
        ):
            raise ValueError("timeout_returncode must be a non-zero integer")
        self._image = _validate_image(
            image if image is not None else options.image if options is not None else "python:3.11-slim"
        )
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
        self._init_process = init_process
        self._timeout_returncode = timeout_returncode
        self._backing_environment = backing_environment
        self._execution_owner: DockerEnvironment | None = None
        self.source_workspace = getattr(backing_environment, "source_workspace", None)
        self.host_workspace = None
        self._resolved_workspace: str | None = None
        self._temporary_files: set[str] = set()

    @property
    def revoked(self) -> bool:
        return self._aborted or (self._execution_owner is not None and self._execution_owner.revoked)

    def _ensure_active(self) -> None:
        if self.revoked:
            raise RuntimeError("Execution environment has been aborted.")

    @property
    def container_reference(self) -> str | None:
        """Return the attached container, or ``None`` for an owned container.

        Per-agent worktrees use this reference to share an attached container.
        """
        return self._attached_reference

    @property
    def command_prefix(self) -> Callable[[str], str] | str | None:
        """Return the wrapper applied to agent commands, if any.

        Other views of this container must use it to share the same runtime setup.
        """
        return self._command_prefix

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
        container_name = f"opencollab-{uuid.uuid4().hex[:16]}"
        args = [
            "run",
            "-d",
            "--network",
            self._options.network,
            "--name",
            container_name,
            "--label",
            f"{DOCKER_OWNER_LABEL}={self._owner_token}",
        ]
        if self._init_process:
            args.append("--init")
        for flag, value in (
            ("--cpus", self._options.cpus), ("--memory", self._options.memory),
            ("--pids-limit", self._options.pids_limit),
        ):
            if value is not None:
                args.extend((flag, str(value)))
        for mount in self._options.mounts:
            source = os.path.realpath(os.path.abspath(mount.source))
            if not os.path.exists(source):
                raise FileNotFoundError(source)
            paths = (
                (os.path.join(directory, name) for directory, _dirs, files in os.walk(source) for name in files)
                if os.path.isdir(source) else (source,)
            )
            if any(stat.S_ISSOCK(os.stat(path).st_mode) for path in paths):
                raise ValueError("Docker mounts cannot expose host sockets")
            if "," in source:
                raise ValueError("Docker mount source cannot contain commas")
            args.extend(("--mount", f"type=bind,source={source},target={mount.target},readonly"))
        if host_mount is not None:
            args.extend(("-v", f"{host_mount}:{self.workspace}"))
        args.extend(("-w", self.workspace, self._image, "sleep", "infinity"))
        self._container_name = container_name
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

    def _exec_argv(
        self,
        cmd: str,
        token: str,
        *,
        interactive: bool = False,
        login: bool | None = None,
    ) -> tuple[str, ...]:
        assert self._container_id is not None
        pidfile = f"/tmp/.opencollab-exec-{token}.pid"
        args = ["exec"]
        if interactive:
            args.append("-i")
        if self._exec_workdir:
            args.extend(("-w", self._exec_workdir))
        for name, value in _GIT_IDENTITY_ENV:
            args.extend(("-e", f"{name}={value}"))
        # File commands opt out of the login shell. Its profile shares stdout
        # with cat and the write receipt, so a banner becomes file bytes.
        use_login = self._command_prefix is not None if login is None else login
        shell_flag = "-lc" if use_login else "-c"
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
            if self._execution_owner is not None:
                self._execution_owner.revoke()
                await self._execution_owner._stop_owned_container()
            return False
        # Losing the command group must not discard the caller's workspace or
        # masquerade as a recoverable tool timeout. Retain it until cleanup().
        self.revoke()
        await self._stop_owned_container()
        return False

    async def _stop_owned_container(self) -> None:
        if self._container_id is None:
            return
        result = await self._docker("stop", "--time", "1", "--", self._container_id)
        if result.returncode != 0:
            raise ProcessCleanupError("owned container could not be stopped; workspace retained")

    async def _exec(
        self,
        cmd: str,
        *,
        timeout: float,
        input_bytes: bytes | None = None,
        strict_stdout: bool = False,
        stdout_byte_limit: int | None = None,
        login: bool | None = None,
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
                *self._exec_argv(
                    cmd, token, interactive=input_bytes is not None, login=login,
                ),
                timeout=timeout,
                input_bytes=input_bytes,
            )
        except asyncio.TimeoutError as exc:
            if not await await_owned_operation(
                self._recover_inner(token),
                propagate_cancellation=True,
            ):
                raise ProcessCleanupError("timed out container command did not quiesce")
            # Return pre-deadline output with the timeout so failures printed before a
            # hang remain visible.
            return timed_out_result(exc, self._timeout_returncode, timeout)
        except asyncio.CancelledError as exc:
            if not await await_owned_operation(self._recover_inner(token)):
                add_exception_note(exc, "cancelled container command did not quiesce")
            raise
        finally:
            async with self._active_exec_lock:
                self._active_execs.pop(token, None)
        decoded = result.to_exec_result()
        if strict_stdout and result.returncode == 0:
            if decoded.stdout_truncated and stdout_byte_limit is not None:
                raise OSError("docker file read exceeded capture limit")
            if not decoded.stdout_truncated:
                if stdout_byte_limit is None:
                    decoded.stdout = result.stdout.decode("utf-8", errors="strict")
                else:
                    # The extra probe byte distinguishes a complete window
                    # from a byte-capped prefix. Retain strict validation while
                    # buffering a partial UTF-8 character only at that cap.
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
                    decoded.stdout = decoder.decode(
                        result.stdout,
                        final=len(result.stdout) <= stdout_byte_limit,
                    )
        return decoded

    async def exec_cmd(self, cmd: str, timeout: float = 120.0, *, input_bytes: bytes | None = None) -> ExecResult:
        return await self._exec(cmd, timeout=timeout, input_bytes=input_bytes)

    async def read_file(self, path: str) -> str:
        physical = await self._contained_file_path(path)
        marker = _output_marker()
        result = await self._exec(
            _framed(marker, f"cat -- {shlex.quote(physical)}"),
            timeout=120.0,
            strict_stdout=True,
            login=False,
        )
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        if result.stdout_truncated:
            raise OSError(f"docker read exceeded capture limit for {path}")
        return _text_after_marker(result.stdout, marker)

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
        # Include a complete character beyond the display budget so an
        # incomplete character buffered at the byte cap still marks truncation.
        byte_cap = max_chars * 4 + limit + 4
        physical = await self._contained_file_path(path)
        quoted_path = shlex.quote(physical)
        marker = _output_marker()
        command = _framed(marker, (
            f"[ -f {quoted_path} ] && [ -r {quoted_path} ] || exit 66; "
            "command -v awk >/dev/null && command -v sed >/dev/null && command -v tr >/dev/null && "
            "command -v head >/dev/null || exit 127; "
            # Strip CR at a physical LF boundary and restore that boundary,
            # including the last record. Then translate remaining CR before
            # selecting numbered logical lines. The C locale preserves bytes.
            f"LC_ALL=C awk '{{ sub(/\\r$/, \"\"); print }}' < {quoted_path} | LC_ALL=C tr '\\015' '\\012' | "
            f"LC_ALL=C sed -n '{offset},{final_line}p;{final_line}q' "
            f"| head -c {byte_cap + 1}"
        ))
        result = await self._exec(
            command,
            timeout=120.0,
            strict_stdout=True,
            stdout_byte_limit=byte_cap + len(marker.encode("utf-8")) + 1,
            login=False,
        )
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        lines, _ended_nl = _split_lines(_normalize_newlines(_text_after_marker(result.stdout, marker)))
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
        await self._write_file(path, content, observe_change=False)

    async def write_file_with_change(self, path: str, content: str) -> bool | None:
        return await self._write_file(path, content, observe_change=True)

    @contextlib.asynccontextmanager
    async def file_write_locks(self, paths: Sequence[str]) -> AsyncIterator[None]:
        self._ensure_active()
        await self._bind_attached()
        if self._container_id is None:
            raise RuntimeError("Container not started. Call setup() first.")
        targets = {
            await self._resolve_write_target(self._validate_container_path(path))
            for path in paths
        }
        physical_workspace = self._resolved_workspace or self.workspace
        ancestors = set()
        for path in paths:
            parent = posixpath.dirname(path)
            while parent not in ("", ".", "/", self.workspace, physical_workspace):
                if posixpath.isabs(parent) and not any(
                    _within_directory(root, posixpath.normpath(parent))
                    for root in (self.workspace, physical_workspace)
                ):
                    break
                if posixpath.basename(parent) not in (".", ".."):
                    ancestors.add(parent)
                parent = posixpath.dirname(parent)
        targets.update({
            await self._resolve_write_target(self._validate_container_path(path))
            for path in ancestors
        })
        # Preserve alias nodes and add physical ancestors before taking any
        # locks, so directory recovery and alias edits share one lock order.
        ordered = _write_lock_paths(targets, self, workspace=physical_workspace)
        async with contextlib.AsyncExitStack() as locks:
            for target in ordered:
                await locks.enter_async_context(self._resolved_file_write_lock(target))
            yield None

    @contextlib.asynccontextmanager
    async def file_write_lock(
        self,
        path: str,
        *,
        containment_root: str | None = None,
    ) -> AsyncIterator[str]:
        """Hold the target lock across a native edit, including its final write."""
        self._ensure_active()
        await self._bind_attached()
        if self._container_id is None:
            raise RuntimeError("Container not started. Call setup() first.")
        target = self._validate_container_path(path)
        target = await self._resolve_write_target(target, containment_root=containment_root)
        async with self._resolved_file_write_lock(target):
            yield target

    @contextlib.asynccontextmanager
    async def _resolved_file_write_lock(self, target: str) -> AsyncIterator[str]:
        key = f"{self._container_id}\0{target}"
        task = asyncio.current_task()
        assert task is not None
        # Native tools already hold this lock when they call write_file. Only
        # the actual owning task can reenter; a spawned task must still wait.
        with _WRITE_LOCKS_GUARD:
            reentrant = _WRITE_LOCK_OWNERS.get(key) is task
            if not reentrant:
                entry = _WRITE_LOCKS.setdefault(key, _WriteLockEntry())
                entry.users += 1
        if reentrant:
            yield target
            return
        acquired = False
        try:
            # Attached containers survive individual event loops, and callers
            # can edit them from different threads. Poll a process-wide lock
            # without blocking either loop or binding it to the first waiter.
            while not entry.lock.acquire(blocking=False):
                await asyncio.sleep(0.02)
            acquired = True
            self._ensure_active()
            with _WRITE_LOCKS_GUARD:
                _WRITE_LOCK_OWNERS[key] = task
            try:
                yield target
            finally:
                with _WRITE_LOCKS_GUARD:
                    _WRITE_LOCK_OWNERS.pop(key, None)
        finally:
            if acquired:
                entry.lock.release()
            with _WRITE_LOCKS_GUARD:
                entry.users -= 1
                if entry.users == 0:
                    del _WRITE_LOCKS[key]

    async def _write_file(
        self,
        path: str,
        content: str,
        *,
        observe_change: bool,
        containment_root: str | None = None,
    ) -> bool | None:
        payload = content.encode("utf-8")
        if len(payload) > ENV_FILE_WRITE_LIMIT_BYTES:
            raise OSError(
                f"docker file exceeds write limit of {ENV_FILE_WRITE_LIMIT_BYTES} bytes: {path}"
            )
        async with self.file_write_lock(path, containment_root=containment_root) as target:
            return await self._write_file_atomic(target, payload, observe_change=observe_change)

    async def _resolve_write_target(
        self,
        target: str,
        *,
        containment_root: str | None = None,
    ) -> str:
        # Resolve the parent through the file execution wrapper, including its
        # cwd and command prefix. Keep the final component so atomic writes
        # replace a file symlink rather than modifying its referent.
        directory, filename = posixpath.split(target)
        command = (
            'directory=$1; workspace=$2; suffix=; '
            'while [ ! -d "$directory" ]; do '
            '[ ! -e "$directory" ] && [ ! -L "$directory" ] || exit 66; '
            'suffix="/${directory##*/}$suffix"; directory=${directory%/*}; '
            '[ -n "$directory" ] || directory=/; done; '
            'cd -P -- "$directory" || exit 66; '
            'phys_ws=$(readlink -f -- "$workspace") || exit 67; '
            'printf "\\0%s\\0%s%s\\0" "$phys_ws" "$PWD" "$suffix"'
        )
        parent = directory or "."
        if not posixpath.isabs(parent):
            parent = "./" + parent
        if containment_root not in (None, "/tmp"):
            raise ValueError("container temporary files must stay under /tmp")
        root = self.workspace if containment_root is None else containment_root
        result = await self._exec(
            "bash -c "
            f"{shlex.quote(command)} opencollab-file-parent {shlex.quote(parent)} "
            f"{shlex.quote(root)}",
            timeout=DOCKER_CONTROL_TIMEOUT_SECONDS,
            login=False,
        )
        parts = result.stdout.rsplit("\0", 3)
        resolved_root = parts[-3] if len(parts) == 4 else ""
        resolved = parts[-2] if len(parts) == 4 else ""
        if (
            result.returncode != 0
            or result.stdout_truncated
            or not posixpath.isabs(resolved_root)
            or not posixpath.isabs(resolved)
        ):
            raise OSError("cannot resolve container file execution directory")
        resolved_root = posixpath.normpath(resolved_root)
        # Owned /tmp files must not replace the cached workspace root. A later
        # workspace write would then treat the temporary directory as the project.
        if containment_root is None:
            self._resolved_workspace = resolved_root
        resolved_target = posixpath.normpath(posixpath.join(resolved, filename))
        if not _within_directory(resolved_root, resolved_target):
            boundary = "workspace" if containment_root is None else "temporary directory"
            raise PermissionError(f"container file path escapes {boundary}: {target}")
        return resolved_target

    async def _physical_workspace(self) -> str:
        if self._resolved_workspace is not None:
            return self._resolved_workspace
        marker = _output_marker()
        command = _framed(marker, f"readlink -f -- {shlex.quote(self.workspace)}")
        result = await self._exec(
            command,
            timeout=DOCKER_CONTROL_TIMEOUT_SECONDS,
            strict_stdout=True,
            login=False,
        )
        text = ""
        if result.returncode == 0 and not result.stdout_truncated:
            try:
                text = _text_after_marker(result.stdout, marker).removesuffix("\n")
            except OSError:
                text = ""
        resolved = posixpath.normpath(text) if text else ""
        if not posixpath.isabs(resolved):
            raise OSError("cannot resolve container workspace")
        self._resolved_workspace = resolved
        return resolved

    async def _symlink_referent(self, path: str) -> str | None:
        marker = _output_marker()
        command = _framed(marker, f"readlink -f -- {shlex.quote(path)} 2>/dev/null || true")
        result = await self._exec(
            command,
            timeout=DOCKER_CONTROL_TIMEOUT_SECONDS,
            strict_stdout=True,
            login=False,
        )
        if result.returncode != 0 or result.stdout_truncated:
            return None
        try:
            text = _text_after_marker(result.stdout, marker).removesuffix("\n")
        except OSError:
            return None
        if not posixpath.isabs(text):
            return None
        return posixpath.normpath(text)

    async def _contained_file_path(self, path: str) -> str:
        """Resolve a file for reading and refuse a symlink that leaves the workspace."""
        target = await self._resolve_write_target(self._validate_container_path(path))
        referent = await self._symlink_referent(target)
        if referent is None:
            return target
        if not _within_directory(await self._physical_workspace(), referent):
            raise PermissionError(f"container file path escapes workspace: {path}")
        return referent

    @staticmethod
    def _validate_container_path(path: str) -> str:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("container file path must be non-empty text without NUL bytes")
        normalized = posixpath.normpath(path)
        if normalized in (".", "/") or posixpath.basename(normalized) in (".", ".."):
            raise ValueError("container file path must name a file")
        # Preserve directory symlinks before .. until the container resolves
        # the physical parent for both the write target and its lock.
        return path

    @staticmethod
    def _content_comparison_command() -> str:
        # A comparison error is unknown and must never prevent the original mv.
        return (
            'change=unknown; identity() { stat -c "%d:%i:%s:%y:%z" -- "$target" 2>/dev/null || '
            'stat -f "%d:%i:%z:%Fm:%Fc" -- "$target" 2>/dev/null; }; '
            'if [ -f "$target" ] && [ ! -L "$target" ]; then '
            'before=$(identity); '
            'target_bytes=$(stat -c %s -- "$target" 2>/dev/null || stat -f %z -- "$target" 2>/dev/null); '
            'case "$target_bytes" in ""|*[!0-9]*) ;; *) '
            'if [ "$target_bytes" != "$expected_bytes" ]; then change=different; '
            'elif cmp -s -- "$temporary" "$target"; then change=equal; '
            'else comparison_status=$?; [ "$comparison_status" -ne 1 ] || change=different; fi ;; esac; '
            'after=$(identity); [ -n "$before" ] && [ "$before" = "$after" ] || change=unknown; '
            'elif [ ! -e "$target" ] && [ ! -L "$target" ]; then change=different; fi; :; '
        )

    async def _write_file_atomic(self, target: str, payload: bytes, *, observe_change: bool = False) -> bool | None:
        digest = hashlib.sha256(payload).hexdigest()
        directory, filename = posixpath.split(target)
        temporary = posixpath.join(
            directory or ".",
            f".{filename}.opencollab-write-{uuid.uuid4().hex}.tmp",
        )
        comparison = '{ ' + self._content_comparison_command() + '} && ' if observe_change else ''
        confirmation = (
            'printf "%s\\t%s\\t%s\\n" "$bytes" "$digest" "$change"'
            if observe_change else 'printf "%s\\t%s\\n" "$bytes" "$digest"')
        # Normalize BSD wc padding before comparing the count with the
        # caller's byte count. Use the same verification for the temporary
        # payload and the final destination returned in the write receipt.
        verification = (
            'bytes=$(wc -c < "$verified" | tr -d \'[:space:]\') && '
            'digest=$(sha256sum -- "$verified" 2>/dev/null | awk \'{print $1}\' || '
            'shasum -a 256 -- "$verified" | awk \'{print $1}\') && '
            '[ "$bytes" = "$expected_bytes" ] && [ "$digest" = "$expected_digest" ] && '
        )
        command = (
            'target=$1; temporary=$2; expected_bytes=$3; expected_digest=$4; '
            'cleanup() { rm -f -- "$temporary"; }; trap cleanup EXIT HUP INT TERM; '
            '[ ! -d "$target" ] && '
            'mkdir -p -- "$(dirname -- "$target")" && '
            '(umask 077; set -C; : > "$temporary") && '
            'cat > "$temporary" && '
            'verified=$temporary && ' + verification + comparison +
            # Read the mode only from a regular target. Replacing a symlink
            # keeps the new-file mode and leaves its referent untouched.
            '{ if [ -f "$target" ] && [ ! -L "$target" ]; then '
            'mode=$(stat -c %a -- "$target" 2>/dev/null || stat -f %Lp -- "$target") && '
            'chmod -- "$mode" "$temporary"; else :; fi; } && '
            '[ ! -d "$target" ] && '
            'mv -f -- "$temporary" "$target" && '
            '[ -f "$target" ] && [ ! -L "$target" ] && '
            'verified=$target && ' + verification +
            'trap - EXIT HUP INT TERM && ' + confirmation
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
                login=False,
            )
            expected = f"{len(payload)}\t{digest}"
            receipt = _write_receipt(result.stdout, expected, observe_change=observe_change)
            if result.returncode != 0 or receipt is None:
                raise OSError(f"docker write verification failed for {target}")
            committed = True
            if not observe_change:
                return None
            return {"equal": False, "different": True}.get(receipt.split("\t")[2])
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
        # Patch staging owns this path. It is outside the project on purpose;
        # workspace containment would reject it and drop the binary patch.
        await self._write_file(path, content, observe_change=False, containment_root="/tmp")
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
            raise RuntimeError("owned Docker container could not be removed")
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
            if not self._attached:
                self.revoke()
                await await_owned_operation(self._cleanup_resources(), propagate_cancellation=True)
                return
            await self._abort_resources_locked()
            await await_owned_operation(
                self._cleanup_attached_resources(),
                propagate_cancellation=True,
            )

    async def abort(self) -> None:
        async with self._lifecycle_lock:
            await self._abort_resources_locked()

    async def _abort_resources_locked(self) -> None:
        """Abort resources while the lifecycle lock is already held."""
        self.revoke()
        if not self._attached:
            await await_owned_operation(
                self._stop_owned_container(),
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
            if self._execution_owner is not None:
                self._execution_owner.revoke()
                await self._execution_owner._stop_owned_container()
            raise ProcessCleanupError(
                "attached container commands did not quiesce during abort"
            )

__all__ = ["DockerEnvironment"]
