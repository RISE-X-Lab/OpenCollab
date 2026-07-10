"""Environment — abstraction over execution contexts.

First Principle: All side effects go through Environment.
- LocalEnvironment: direct OS execution (daily dev)
- WorktreeEnvironment: isolated git worktree per spawned agent (parallel agents)
- DockerEnvironment: container sandbox (eval / SWE-bench)

Ref:
- Design doc: Environment base class with exec_cmd/read_file/write_file
- openclaw: sandbox.ts workspaceDir + readonlyRoots
- User feedback: parallel spawned agents MUST use separate physical workspaces (git worktree)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROCESS_TERM_GRACE_SECONDS = 1.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 2.0
PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS = 2.0
DOCKER_SETUP_TIMEOUT_SECONDS = 120.0
DOCKER_COMPENSATION_TIMEOUT_SECONDS = 10.0
DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS = 5.0
DOCKER_WRITE_TIMEOUT_SECONDS = 120.0
DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE = 197
WORKTREE_GIT_TIMEOUT_SECONDS = 30.0
PROCESS_OUTPUT_CAPTURE_BYTES = 1_048_576
PROCESS_IO_JOIN_TIMEOUT_SECONDS = 1.0
LOCAL_FILE_READ_LIMIT_BYTES = 4_194_304
LOCAL_FILE_WRITE_LIMIT_BYTES = 4_194_304
DOCKER_OWNER_LABEL = "opencollab.harness.owner-token"
DOCKER_REFERENCE_MAX_BYTES = 512
DOCKER_CONTAINER_NAME_MAX_BYTES = 255
_DOCKER_MISSING_RE = re.compile(rb"no such (?:container|object)", re.IGNORECASE)
_DOCKER_INSPECT_FORMAT = (
    '{{.Id}}{{printf "\\t"}}{{.Name}}{{printf "\\t"}}'
    '{{index .Config.Labels "' + DOCKER_OWNER_LABEL + '"}}'
)
_DOCKER_ATTACH_INSPECT_FORMAT = (
    '{{.Id}}{{printf "\\t"}}{{.Name}}{{printf "\\t"}}{{.State.Running}}'
)

_PROCESS_POPEN = subprocess.Popen


def _validate_docker_image_reference(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Docker image reference must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Docker image reference must be valid UTF-8") from exc
    if (
        len(encoded) > DOCKER_REFERENCE_MAX_BYTES
        or value.startswith("-")
        or "://" in value
        or any(character.isspace() for character in value)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", value) is None
    ):
        raise ValueError("Docker image reference is unsafe or malformed")
    return value


def _validate_docker_container_reference(value: object) -> str:
    """Accept an unambiguous full id or a bounded Docker container name."""
    if not isinstance(value, str) or not value:
        raise ValueError("Docker container reference must be non-empty text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Docker container reference must be ASCII") from exc
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    if (
        len(encoded) > DOCKER_CONTAINER_NAME_MAX_BYTES
        or value.startswith("-")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None
        or re.fullmatch(r"[0-9a-fA-F]+", value) is not None
    ):
        raise ValueError("Docker container reference is unsafe or ambiguous")
    return value

_DOCKER_EXEC_WRAPPER = r"""
pidfile=$1
cancelfile=$2
shellflag=$3
command=$4
shift 4
cleanup() { rm -f -- "$pidfile" "$cancelfile"; }
terminate_child() {
    kill -TERM -- "-$child" 2>/dev/null || true
    probe=0
    while [ "$probe" -lt 10 ] && kill -0 -- "-$child" 2>/dev/null; do
        sleep 0.05
        probe=$((probe + 1))
    done
    if kill -0 -- "-$child" 2>/dev/null; then
        kill -KILL -- "-$child" 2>/dev/null || true
    fi
    wait "$child" 2>/dev/null || true
    probe=0
    while [ "$probe" -lt 20 ] && kill -0 -- "-$child" 2>/dev/null; do
        sleep 0.05
        probe=$((probe + 1))
    done
    if kill -0 -- "-$child" 2>/dev/null; then
        return 1
    fi
    return 0
}
if [ -e "$cancelfile" ]; then
    cleanup
    exit 143
fi
if command -v setsid >/dev/null 2>&1; then
    setsid bash "$shellflag" "$command" "$@" <&0 &
else
    set -m
    bash "$shellflag" "$command" "$@" <&0 &
fi
child=$!
if ! printf '%s\n' "$child" > "$pidfile"; then
    if terminate_child; then
        cleanup
        exit 125
    fi
    exit 197
fi
if [ -e "$cancelfile" ]; then
    if terminate_child; then
        cleanup
        exit 143
    fi
    exit 197
fi
wait "$child"
status=$?
if kill -0 -- "-$child" 2>/dev/null; then
    if terminate_child; then
        cleanup
        exit 125
    fi
    exit 197
fi
cleanup
exit "$status"
""".strip()

_DOCKER_EXEC_CANCEL = r"""
pidfile=$1
cancelfile=$2
if ! : > "$cancelfile"; then
    exit 125
fi
attempt=0
while [ "$attempt" -lt 20 ]; do
    if read -r child < "$pidfile" 2>/dev/null; then
        case "$child" in
            ''|*[!0-9]*) exit 125 ;;
        esac
        kill -TERM -- "-$child" 2>/dev/null || true
        probe=0
        while [ "$probe" -lt 2 ] && kill -0 -- "-$child" 2>/dev/null; do
            sleep 0.05
            probe=$((probe + 1))
        done
        if kill -0 -- "-$child" 2>/dev/null; then
            kill -KILL -- "-$child" 2>/dev/null || true
        fi
        probe=0
        while [ "$probe" -lt 20 ] && kill -0 -- "-$child" 2>/dev/null; do
            sleep 0.05
            probe=$((probe + 1))
        done
        if kill -0 -- "-$child" 2>/dev/null; then
            exit 124
        fi
        exit 0
    fi
    sleep 0.05
    attempt=$((attempt + 1))
done
exit 124
""".strip()

_DOCKER_WRITE_AND_VERIFY = r"""
target=$1
mkdir -p -- "$(dirname -- "$target")" || exit 73
cat > "$target" || exit 74
bytes=$(wc -c < "$target") || exit 74
bytes=${bytes//[[:space:]]/}
if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum -- "$target" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 -- "$target" | awk '{print $1}')
elif command -v openssl >/dev/null 2>&1; then
    digest=$(openssl dgst -sha256 "$target" | awk '{print $NF}')
else
    exit 69
fi
case "$bytes" in
    ''|*[!0-9]*) exit 65 ;;
esac
case "$digest" in
    ''|*[!0-9a-f]*) exit 65 ;;
esac
printf '%s\t%s\n' "$bytes" "$digest"
""".strip()

_DOCKER_CREATE_WRITE_AND_VERIFY = r"""
target=$1
umask 077
set -o noclobber
if ! exec 3> "$target"; then
    exit 73
fi
set +o noclobber
owned_identity=$(stat -Lc '%d:%i' /proc/self/fd/3 2>/dev/null) || exit 69
cleanup_owned() {
    current=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || return 0
    if [ "$current" = "$owned_identity" ]; then
        rm -f -- "$target"
    fi
}
trap 'cleanup_owned' EXIT HUP INT TERM
cat >&3 || exit 74
bytes=$(wc -c < /proc/self/fd/3) || exit 74
bytes=${bytes//[[:space:]]/}
if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum -- /proc/self/fd/3 | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 -- /proc/self/fd/3 | awk '{print $1}')
elif command -v openssl >/dev/null 2>&1; then
    digest=$(openssl dgst -sha256 /proc/self/fd/3 | awk '{print $NF}')
else
    exit 69
fi
case "$bytes" in
    ''|*[!0-9]*) exit 65 ;;
esac
case "$digest" in
    ''|*[!0-9a-f]*) exit 65 ;;
esac
current_identity=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || exit 75
if [ "$current_identity" != "$owned_identity" ]; then
    exit 75
fi
trap - EXIT HUP INT TERM
exec 3>&-
printf '%s\t%s\t%s\n' "$owned_identity" "$bytes" "$digest"
""".strip()

_DOCKER_REMOVE_OWNED_TEMP = r"""
target=$1
expected=$2
current=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || exit 0
if [ "$current" != "$expected" ]; then
    exit 76
fi
rm -f -- "$target"
""".strip()


def _positive_finite_timeout(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number of seconds")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a positive finite number of seconds"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
    return timeout


def _positive_file_size_limit(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer number of bytes")
    return value


def _open_regular_file_flags(access: int) -> int:
    return (
        access
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_parent_dirfd(path: str, *, create_parents: bool) -> tuple[int, str]:
    """Resolve every ancestor from ``/`` without following symbolic links."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ValueError("local file path must be absolute text without NUL bytes")
    normalized = os.path.normpath(path)
    components = normalized.split(os.sep)[1:]
    if not components or not components[-1] or components[-1] in {".", ".."}:
        raise OSError(f"local file path has no file component: {path}")
    parent_components = components[:-1]
    current_fd = os.open(os.sep, _directory_open_flags())
    try:
        for component in parent_components:
            if component in {"", ".", ".."}:
                raise OSError(f"unsafe local path component: {path}")
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    os.mkdir(component, 0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, components[-1]
    except BaseException:
        os.close(current_fd)
        raise


def _lstat_for_nofollow_compat(
    parent_fd: int,
    name: str,
    path: str,
) -> os.stat_result | None:
    """Capture the final component when the platform lacks ``O_NOFOLLOW``."""
    if getattr(os, "O_NOFOLLOW", 0):
        return None
    result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise OSError(f"refusing to open symbolic link: {path}")
    return result


def _verify_opened_regular_file(
    fd: int,
    path: str,
    before: os.stat_result | None,
) -> None:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(f"refusing to access non-regular file: {path}")
    if before is not None and (
        opened.st_dev != before.st_dev or opened.st_ino != before.st_ino
    ):
        raise OSError(f"file changed while opening without O_NOFOLLOW support: {path}")


def _verify_path_still_names_open_file(
    parent_fd: int,
    name: str,
    fd: int,
    path: str,
) -> None:
    opened = os.fstat(fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise OSError(f"local file path changed during access: {path}")


def _sync_read_regular_file(path: str, limit: int) -> bytes:
    parent_fd, name = _open_parent_dirfd(path, create_parents=False)
    try:
        before = _lstat_for_nofollow_compat(parent_fd, name, path)
        fd = os.open(
            name,
            _open_regular_file_flags(os.O_RDONLY),
            dir_fd=parent_fd,
        )
        try:
            _verify_opened_regular_file(fd, path, before)
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limit:
                raise OSError(f"local file exceeds read limit of {limit} bytes: {path}")
            _verify_path_still_names_open_file(parent_fd, name, fd, path)
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _sync_write_regular_file(path: str, payload: bytes) -> None:
    parent_fd, name = _open_parent_dirfd(path, create_parents=True)
    flags = _open_regular_file_flags(os.O_WRONLY)
    try:
        try:
            before = _lstat_for_nofollow_compat(parent_fd, name, path)
        except FileNotFoundError:
            before = None
        if before is None and not getattr(os, "O_NOFOLLOW", 0):
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o666,
                dir_fd=parent_fd,
            )
        else:
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                fd = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o666,
                    dir_fd=parent_fd,
                )
                before = None
        try:
            _verify_opened_regular_file(fd, path, before)
            os.ftruncate(fd, 0)
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset : offset + 65_536])
                if written <= 0:
                    raise OSError(f"short write while writing local file: {path}")
                offset += written
            os.fsync(fd)
            _verify_path_still_names_open_file(parent_fd, name, fd, path)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _sync_unlink_file(path: str, expected_identity: tuple[int, int] | None) -> None:
    parent_fd, name = _open_parent_dirfd(path, create_parents=False)
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if expected_identity is not None and (
            current.st_dev,
            current.st_ino,
        ) != expected_identity:
            raise OSError(f"refusing to remove replaced local temporary file: {path}")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _sync_create_temp_file(
    temp_dir: str,
    prefix: str,
    suffix: str,
    payload: bytes,
) -> tuple[str, tuple[int, int]]:
    canonical_temp_dir = os.path.realpath(temp_dir)
    parent_fd, directory_name = _open_parent_dirfd(
        canonical_temp_dir,
        create_parents=False,
    )
    try:
        directory_fd = os.open(
            directory_name,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        for candidate_part in tempfile._get_candidate_names():
            candidate = f"{prefix}{candidate_part}{suffix}"
            try:
                fd = os.open(
                    candidate,
                    _open_regular_file_flags(os.O_WRONLY)
                    | os.O_CREAT
                    | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset : offset + 65_536])
                    if written <= 0:
                        raise OSError("short write while staging temporary file")
                    offset += written
                os.fsync(fd)
                _verify_path_still_names_open_file(
                    directory_fd,
                    candidate,
                    fd,
                    os.path.join(canonical_temp_dir, candidate),
                )
                opened = os.fstat(fd)
                identity = (opened.st_dev, opened.st_ino)
            except BaseException:
                try:
                    current = os.stat(
                        candidate,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    opened = os.fstat(fd)
                    if (current.st_dev, current.st_ino) == (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        os.unlink(candidate, dir_fd=directory_fd)
                except (FileNotFoundError, OSError):
                    pass
                raise
            finally:
                os.close(fd)
            return os.path.join(canonical_temp_dir, candidate), identity
        raise FileExistsError("could not allocate exclusive local temporary file")
    finally:
        os.close(directory_fd)


async def _await_owned_transaction(awaitable, *, failure_note: str):
    """Finish every stage of an owned transaction before propagating cancel."""
    worker = asyncio.ensure_future(awaitable)
    first_cancellation: asyncio.CancelledError | None = None
    operation_error: BaseException | None = None
    result: object = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                raise
            if first_cancellation is None:
                first_cancellation = exc
        except BaseException as exc:
            operation_error = exc
            break

    if first_cancellation is not None:
        if operation_error is not None:
            add_note = getattr(first_cancellation, "add_note", None)
            if callable(add_note):
                add_note(
                    f"{failure_note} failed after cancellation: "
                    f"{type(operation_error).__name__}: {operation_error}"
                )
        raise first_cancellation
    if operation_error is not None:
        raise operation_error
    return result


async def _run_owned_blocking_io(operation: Callable[..., object], *args: object):
    """Keep blocking host I/O off the loop and finish owned writes on cancel."""
    return await _await_owned_transaction(
        asyncio.to_thread(operation, *args),
        failure_note="owned file operation",
    )


class _BoundedCapture:
    def __init__(self, limit: int):
        self._limit = max(256, int(limit))
        self._head_limit = self._limit // 2
        self._tail_limit = self._limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._total += len(chunk)
            remaining_head = self._head_limit - len(self._head)
            if remaining_head > 0:
                self._head.extend(chunk[:remaining_head])
                chunk = chunk[remaining_head:]
            if chunk:
                self._tail.extend(chunk)
                if len(self._tail) > self._tail_limit:
                    del self._tail[: len(self._tail) - self._tail_limit]

    def render(self) -> tuple[bytes, int]:
        with self._lock:
            kept = len(self._head) + len(self._tail)
            dropped = max(0, self._total - kept)
            if dropped == 0:
                return bytes(self._head + self._tail), 0
            marker = (
                f"\n...[opencollab truncated {dropped} bytes]...\n".encode()
            )
            return bytes(self._head) + marker + bytes(self._tail), dropped


def _sync_process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.error("cannot probe process group %s: %s", pgid, exc)
        return True
    return True


def _sync_wait_for_process_group_exit(pgid: int, *, deadline: float) -> bool:
    while _sync_process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _sync_signal_process_group(
    proc: subprocess.Popen[bytes],
    sig: signal.Signals,
) -> bool:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        try:
            if proc.poll() is not None:
                return True
        except OSError:
            return False
        action = proc.terminate if sig is signal.SIGTERM else proc.kill
        try:
            action()
        except ProcessLookupError:
            return True
        except OSError as exc:
            logger.error("cannot signal process %s: %s", proc.pid, exc)
            return False
    except PermissionError:
        action = proc.terminate if sig is signal.SIGTERM else proc.kill
        try:
            action()
        except ProcessLookupError:
            return True
        except OSError as exc:
            logger.error("cannot signal process %s: %s", proc.pid, exc)
            return False
    except OSError as exc:
        logger.error("cannot signal process group %s: %s", proc.pid, exc)
        return False
    return True


def _sync_terminate_process_group(proc: subprocess.Popen[bytes]) -> bool:
    try:
        term_timeout = _positive_finite_timeout(
            PROCESS_TERM_GRACE_SECONDS,
            name="PROCESS_TERM_GRACE_SECONDS",
        )
        kill_timeout = _positive_finite_timeout(
            PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
        )
        pgid = proc.pid
        leader_reaped = proc.poll() is not None
        if leader_reaped and not _sync_process_group_exists(pgid):
            return True

        term_signal_ok = _sync_signal_process_group(proc, signal.SIGTERM)
        term_deadline = time.monotonic() + term_timeout
        if not leader_reaped:
            try:
                proc.wait(timeout=max(0.0, term_deadline - time.monotonic()))
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
            except OSError as exc:
                logger.error("cannot reap process %s after SIGTERM: %s", pgid, exc)
        group_gone = _sync_wait_for_process_group_exit(
            pgid,
            deadline=term_deadline,
        )
        if leader_reaped and group_gone and term_signal_ok:
            return True

        kill_signal_ok = _sync_signal_process_group(proc, signal.SIGKILL)
        kill_deadline = time.monotonic() + kill_timeout
        if not leader_reaped:
            try:
                proc.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
            except OSError as exc:
                logger.error("cannot reap process %s after SIGKILL: %s", pgid, exc)
        group_gone = _sync_wait_for_process_group_exit(
            pgid,
            deadline=kill_deadline,
        )
        return leader_reaped and group_gone and kill_signal_ok
    except BaseException as exc:
        logger.error("process-group cleanup failed closed: %s", exc)
        return False


def _sync_run_cleanup_command(
    command: list[str],
    *,
    cwd: str | None,
    timeout: object,
    timeout_name: str,
) -> tuple[int, bytes, bytes, bool]:
    timeout_seconds = _positive_finite_timeout(timeout, name=timeout_name)
    owner = _ThreadProcessOwner(
        command,
        shell=False,
        cwd=cwd,
        timeout=timeout_seconds,
        input_data=None,
        late_compensation=None,
    )
    owner.start()
    wait_bound = (
        timeout_seconds
        + _positive_finite_timeout(
            PROCESS_TERM_GRACE_SECONDS,
            name="PROCESS_TERM_GRACE_SECONDS",
        )
        + _positive_finite_timeout(
            PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
        )
        + PROCESS_IO_JOIN_TIMEOUT_SECONDS * 5
        + 1.0
    )
    completed = owner.finished.wait(wait_bound)
    if not completed:
        owner.cancel()
        return 124, b"", b"process owner still cleaning up", False
    result = owner.result
    if result.error is not None:
        raise result.error
    return (
        124 if result.timed_out else result.returncode or 0,
        result.stdout,
        result.stderr,
        result.cleanup_quiesced,
    )


@dataclass
class _ThreadProcessResult:
    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    cleanup_quiesced: bool = True
    error: BaseException | None = None
    compensation_error: BaseException | None = None
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


class _ThreadProcessOwner:
    def __init__(
        self,
        command: str | list[str],
        *,
        shell: bool,
        cwd: str | None,
        timeout: float,
        input_data: bytes | None,
        late_compensation: Callable[["_ThreadProcessResult"], None] | None,
    ):
        self._command = command
        self._shell = shell
        self._cwd = cwd
        self._timeout = timeout
        self._input_data = input_data
        self._late_compensation = late_compensation
        self._compensation_lock = threading.Lock()
        self._compensation_ran = False
        self._compensation_finished = threading.Event()
        if late_compensation is None:
            self._compensation_finished.set()
        self._cancel_requested = threading.Event()
        self._finished = threading.Event()
        self.result = _ThreadProcessResult()
        self._thread = threading.Thread(
            target=self._run,
            name=f"opencollab-process-owner-{uuid.uuid4().hex[:8]}",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_requested.set()

    @property
    def finished(self) -> threading.Event:
        return self._finished

    def _drain_pipe(
        self,
        pipe,
        capture: _BoundedCapture,
        error_box: list[BaseException],
    ) -> None:
        try:
            while True:
                chunk = pipe.read(65_536)
                if not chunk:
                    break
                capture.append(chunk)
        except BaseException as exc:
            error_box.append(exc)

    def _write_stdin(
        self,
        pipe,
        error_box: list[BaseException],
    ) -> None:
        try:
            if self._input_data:
                pipe.write(self._input_data)
                pipe.flush()
        except (BrokenPipeError, OSError) as exc:
            error_box.append(exc)
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _claim_compensation(
        self,
    ) -> Callable[["_ThreadProcessResult"], None] | None:
        with self._compensation_lock:
            if self._compensation_ran:
                return None
            self._compensation_ran = True
            return self._late_compensation

    def _execute_compensation(
        self,
        callback: Callable[["_ThreadProcessResult"], None],
    ) -> None:
        try:
            callback(self.result)
        except BaseException as exc:
            self.result.compensation_error = exc
        finally:
            self._compensation_finished.set()

    def _run_compensation(self) -> None:
        callback = self._claim_compensation()
        if callback is None:
            return
        self._execute_compensation(callback)

    def start_compensation_thread(self) -> threading.Event:
        callback = self._claim_compensation()
        if callback is None:
            return self._compensation_finished
        threading.Thread(
            target=self._execute_compensation,
            args=(callback,),
            name=f"opencollab-compensation-{uuid.uuid4().hex[:8]}",
            daemon=False,
        ).start()
        return self._compensation_finished

    def _run(self) -> None:
        stdout_capture = _BoundedCapture(PROCESS_OUTPUT_CAPTURE_BYTES)
        stderr_capture = _BoundedCapture(PROCESS_OUTPUT_CAPTURE_BYTES)
        io_errors: list[BaseException] = []
        readers: list[threading.Thread] = []
        writer: threading.Thread | None = None
        proc: subprocess.Popen[bytes] | None = None
        deadline = time.monotonic() + self._timeout
        interrupted = False
        try:
            proc = _PROCESS_POPEN(
                self._command,
                shell=self._shell,
                cwd=self._cwd,
                stdin=subprocess.PIPE
                if self._input_data is not None
                else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            if proc.stdout is not None:
                readers.append(
                    threading.Thread(
                        target=self._drain_pipe,
                        args=(proc.stdout, stdout_capture, io_errors),
                        daemon=True,
                    )
                )
            if proc.stderr is not None:
                readers.append(
                    threading.Thread(
                        target=self._drain_pipe,
                        args=(proc.stderr, stderr_capture, io_errors),
                        daemon=True,
                    )
                )
            for reader in readers:
                reader.start()
            if proc.stdin is not None:
                writer = threading.Thread(
                    target=self._write_stdin,
                    args=(proc.stdin, io_errors),
                    daemon=True,
                )
                writer.start()

            while proc.poll() is None:
                if self._cancel_requested.is_set():
                    interrupted = True
                    break
                if io_errors:
                    interrupted = True
                    self.result.error = io_errors[0]
                    break
                if time.monotonic() >= deadline:
                    interrupted = True
                    self.result.timed_out = True
                    break
                time.sleep(0.01)

            if proc.poll() is not None and _sync_process_group_exists(proc.pid):
                interrupted = True
                self.result.error = OSError(
                    "process leader exited while descendants remained alive"
                )
            if interrupted:
                self.result.cleanup_quiesced = _sync_terminate_process_group(proc)
            else:
                self.result.cleanup_quiesced = True
            self.result.returncode = proc.poll()
        except BaseException as exc:
            self.result.error = exc
            if proc is not None:
                self.result.cleanup_quiesced = _sync_terminate_process_group(proc)
        finally:
            if writer is not None:
                writer.join(timeout=PROCESS_IO_JOIN_TIMEOUT_SECONDS)
                if writer.is_alive() and proc is not None and proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                    writer.join(timeout=PROCESS_IO_JOIN_TIMEOUT_SECONDS)
            for reader in readers:
                reader.join(timeout=PROCESS_IO_JOIN_TIMEOUT_SECONDS)
            if proc is not None:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
            for reader in readers:
                if reader.is_alive():
                    reader.join(timeout=PROCESS_IO_JOIN_TIMEOUT_SECONDS)
            lingering_io = (
                writer is not None and writer.is_alive()
            ) or any(reader.is_alive() for reader in readers)
            if self.result.error is None and io_errors:
                self.result.error = io_errors[0]
            if self.result.error is None and lingering_io:
                self.result.error = OSError(
                    "subprocess pipes did not reach EOF within the drain bound"
                )
            (
                self.result.stdout,
                self.result.stdout_dropped_bytes,
            ) = stdout_capture.render()
            (
                self.result.stderr,
                self.result.stderr_dropped_bytes,
            ) = stderr_capture.render()
            if interrupted or self.result.error is not None:
                self._run_compensation()
            self._finished.set()


async def _wait_thread_event(event: threading.Event, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))
    return True


async def _run_thread_owned_process(
    command: str | list[str],
    *,
    shell: bool,
    cwd: str | None,
    timeout: object,
    timeout_name: str,
    input_data: bytes | None = None,
    late_compensation: Callable[["_ThreadProcessResult"], None] | None = None,
) -> _ThreadProcessResult:
    timeout_seconds = _positive_finite_timeout(timeout, name=timeout_name)
    owner = _ThreadProcessOwner(
        command,
        shell=shell,
        cwd=cwd,
        timeout=timeout_seconds,
        input_data=input_data,
        late_compensation=late_compensation,
    )
    owner.start()
    wait_bound = (
        timeout_seconds
        + _positive_finite_timeout(
            PROCESS_TERM_GRACE_SECONDS,
            name="PROCESS_TERM_GRACE_SECONDS",
        )
        + _positive_finite_timeout(
            PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
        )
        + PROCESS_IO_JOIN_TIMEOUT_SECONDS * 5
        + 1.0
    )
    try:
        completed = await _wait_thread_event(owner.finished, timeout=wait_bound)
    except asyncio.CancelledError as original:
        owner.cancel()
        compensation_completed = False
        cancel_bound = (
            _positive_finite_timeout(
                PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
                name="PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_TERM_GRACE_SECONDS,
                name="PROCESS_TERM_GRACE_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_KILL_REAP_TIMEOUT_SECONDS,
                name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
            )
            + PROCESS_IO_JOIN_TIMEOUT_SECONDS * 5
        )
        try:
            completed = await _await_owned_operation(
                _wait_thread_event(owner.finished, timeout=cancel_bound)
            )
        except BaseException as exc:
            completed = False
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                add_note(f"process owner wait failed: {type(exc).__name__}: {exc}")
        if not completed:
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                add_note("process owner continues cleanup in a non-daemon thread")
        else:
            compensation_event = owner.start_compensation_thread()
            try:
                compensation_completed = await _await_owned_operation(
                    _wait_thread_event(
                        compensation_event,
                        timeout=_positive_finite_timeout(
                            PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
                            name="PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS",
                        ),
                    )
                )
            except BaseException as exc:
                compensation_completed = False
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(
                        "process compensation observation failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        try:
            original.cleanup_quiesced = (
                completed
                and compensation_completed
                and owner.result.cleanup_quiesced
                and owner.result.compensation_error is None
            )
        except (AttributeError, TypeError):
            pass
        if (
            completed
            and compensation_completed
            and owner.result.compensation_error is not None
        ):
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                compensation_error = owner.result.compensation_error
                add_note(
                    "process compensation failed: "
                    f"{type(compensation_error).__name__}: {compensation_error}"
                )
        raise original
    except BaseException as original:
        owner.cancel()
        compensation_completed = False
        cancel_bound = (
            _positive_finite_timeout(
                PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
                name="PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_TERM_GRACE_SECONDS,
                name="PROCESS_TERM_GRACE_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_KILL_REAP_TIMEOUT_SECONDS,
                name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
            )
            + PROCESS_IO_JOIN_TIMEOUT_SECONDS * 5
        )
        try:
            completed = await _await_owned_operation(
                _wait_thread_event(owner.finished, timeout=cancel_bound)
            )
        except BaseException as exc:
            completed = False
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                add_note(f"process owner wait failed: {type(exc).__name__}: {exc}")
        if not completed:
            logger.error(
                "process owner continues cleanup after %s",
                type(original).__name__,
            )
        else:
            compensation_event = owner.start_compensation_thread()
            try:
                compensation_completed = await _await_owned_operation(
                    _wait_thread_event(
                        compensation_event,
                        timeout=_positive_finite_timeout(
                            PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
                            name="PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS",
                        ),
                    )
                )
            except BaseException as exc:
                compensation_completed = False
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(
                        "process compensation observation failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        try:
            original.cleanup_quiesced = (
                completed
                and compensation_completed
                and owner.result.cleanup_quiesced
                and owner.result.compensation_error is None
            )
        except (AttributeError, TypeError):
            pass
        if (
            completed
            and compensation_completed
            and owner.result.compensation_error is not None
        ):
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                compensation_error = owner.result.compensation_error
                add_note(
                    "process compensation failed: "
                    f"{type(compensation_error).__name__}: {compensation_error}"
                )
        raise original
    if not completed:
        owner.cancel()
        raise _OwnedProcessTimeout(cleanup_quiesced=False)
    result = owner.result
    if result.error is not None:
        if not result.cleanup_quiesced or result.compensation_error is not None:
            not_quiesced = _OwnedProcessNotQuiesced(
                "subprocess failed and owned cleanup did not quiesce",
                cleanup_quiesced=False,
            )
            add_note = getattr(not_quiesced, "add_note", None)
            if callable(add_note):
                add_note(
                    f"original process error: {type(result.error).__name__}: "
                    f"{result.error}"
                )
            raise not_quiesced from result.error
        if result.compensation_error is not None:
            add_note = getattr(result.error, "add_note", None)
            if callable(add_note):
                add_note(
                    "process compensation failed: "
                    f"{type(result.compensation_error).__name__}: "
                    f"{result.compensation_error}"
                )
        raise result.error
    if result.timed_out:
        if result.compensation_error is not None:
            logger.error(
                "process timeout compensation failed: %s",
                result.compensation_error,
            )
        raise _OwnedProcessTimeout(
            cleanup_quiesced=(
                result.cleanup_quiesced
                and result.compensation_error is None
            )
        )
    if not result.cleanup_quiesced or result.returncode is None:
        raise _OwnedProcessNotQuiesced(
            "process leader or descendants did not quiesce; "
            "execution result is indeterminate",
            cleanup_quiesced=result.cleanup_quiesced,
        )
    return result


class _OwnedProcessTimeout(asyncio.TimeoutError):
    def __init__(self, *, cleanup_quiesced: bool):
        super().__init__()
        self.cleanup_quiesced = cleanup_quiesced


class _OwnedProcessNotQuiesced(OSError):
    def __init__(self, message: str, *, cleanup_quiesced: bool):
        super().__init__(message)
        self.cleanup_quiesced = cleanup_quiesced


async def _await_owned_operation(awaitable):
    """Finish an owned teardown operation despite repeated caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            # The surrounding operation already owns resource teardown. Further
            # caller cancellations are remembered by the caller's original
            # exception and must not interrupt TERM/KILL/reap or compensation.
            continue


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


class Environment:
    """Abstract execution environment. All tools operate through this."""

    workspace: str = "."
    host_workspace: str | None = None
    source_workspace: str | None = None
    local_filesystem: bool = False
    process_isolated: bool = False
    _aborted: bool = False

    def _ensure_active(self) -> None:
        if self._aborted:
            raise RuntimeError("Execution environment has been aborted.")

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        raise NotImplementedError

    async def read_file(self, path: str) -> str:
        raise NotImplementedError

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        """Create one call-owned temporary file for harness control data."""
        raise NotImplementedError(
            "environment adapters must implement exclusive temporary-file creation"
        )

    async def remove_file(self, path: str) -> None:
        result = await self.exec_cmd(
            f"rm -f -- {shlex.quote(path)}",
            timeout=10.0,
        )
        if (
            result.returncode != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise OSError(f"failed to remove environment file: {path}")

    async def cleanup(self) -> None:
        """Release every owned resource; implementations must be idempotent.

        Returning certifies that cleanup finished.  If cancellation interrupts
        an attempt, the harness may call this method again from the resource's
        setup owner, including while an ``asyncio.run`` loop is shutting down.
        """
        pass

    async def abort(self) -> None:
        """Synchronously revoke future effects, then stop owned activity.

        Implementations must be idempotent because the setup owner retries an
        attempt interrupted by cancellation before it considers the resource
        released.
        """
        self._aborted = True


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
            raise OSError(
                f"local temporary file exceeds write limit of {limit} bytes"
            )
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


class WorktreeEnvironment(Environment):
    """Isolated git worktree — for parallel spawned-agent execution.

    Each spawned agent gets a separate physical copy of the repo via
    `git worktree add`. After task completion, changes are collected as a
    diff patch.

    This solves the concurrency problem: spawned agents cannot corrupt each
    other's git state, env variables, or file locks.

    Ref: User feedback on blind spot #2 — parallel delegation must use
    separate physical workspaces, not just file locks.
    """

    local_filesystem = True

    def __init__(self, source_workspace: str, branch_name: str | None = None):
        self._source = os.path.abspath(source_workspace)
        self.source_workspace = self._source
        # Use UUID to guarantee uniqueness even under parallel same-role delegation
        self._branch = branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        self._worktree_dir: str | None = None
        self._local_env: LocalEnvironment | None = None
        self._base_commit: str | None = None
        self._worktree_registered = False
        self._worktree_add_attempted = False
        self._branch_preexisting: bool | None = None
        self._branch_cleanup_pending = False
        self._add_owner_active = False

    async def _run_git(
        self,
        *args: str,
        late_compensation: Callable[["_ThreadProcessResult"], None] | None = None,
    ) -> ExecResult:
        timeout = _positive_finite_timeout(
            WORKTREE_GIT_TIMEOUT_SECONDS,
            name="WORKTREE_GIT_TIMEOUT_SECONDS",
        )
        try:
            result = await _run_thread_owned_process(
                ["git", *args],
                shell=False,
                cwd=self._source,
                timeout=timeout,
                timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                late_compensation=late_compensation,
            )
        except _OwnedProcessNotQuiesced:
            self._aborted = True
            raise
        except _OwnedProcessTimeout as exc:
            if not exc.cleanup_quiesced:
                self._aborted = True
            raise RuntimeError(
                f"git {' '.join(args)} timed out after {timeout:g}s; "
                f"cleanup_quiesced={exc.cleanup_quiesced}"
            ) from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            raise
        return ExecResult(
            returncode=result.returncode or 0,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            stdout_truncated=result.stdout_dropped_bytes > 0,
            stderr_truncated=result.stderr_dropped_bytes > 0,
            stdout_dropped_bytes=result.stdout_dropped_bytes,
            stderr_dropped_bytes=result.stderr_dropped_bytes,
        )

    async def setup(self) -> str:
        self._ensure_active()
        try:
            return await self._setup()
        except BaseException as original:
            try:
                await _await_owned_operation(self.cleanup())
            except BaseException as cleanup_exc:
                detail = (
                    "worktree setup compensation failed: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
                logger.error(detail)
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise original

    async def _setup(self) -> str:
        """Create the worktree. Returns the worktree directory path."""
        self._ensure_active()
        repo_probe = await self._run_git("rev-parse", "--git-dir")
        if repo_probe.stdout_truncated or repo_probe.stderr_truncated:
            raise RuntimeError("git repository probe output was truncated")
        if repo_probe.returncode != 0:
            detail = repo_probe.stderr.strip()
            raise RuntimeError(
                "worktree isolation requires a Git repository"
                + (f": {detail}" if detail else "")
            )

        branch_probe = await self._run_git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{self._branch}",
        )
        if branch_probe.returncode not in (0, 1):
            raise RuntimeError(
                f"cannot probe worktree branch: {branch_probe.stderr.strip()}"
            )
        self._branch_preexisting = branch_probe.returncode == 0
        if self._branch_preexisting:
            raise RuntimeError(
                f"git worktree add failed: branch {self._branch} already exists"
            )

        base = await self._run_git("rev-parse", "HEAD")
        if base.stdout_truncated or base.stderr_truncated:
            raise RuntimeError("git base commit output was truncated")
        if base.returncode != 0:
            raise RuntimeError(
                f"cannot record worktree base commit: {base.stderr.strip()}"
            )
        self._base_commit = base.stdout.strip()

        def compensate_claim(result: _ThreadProcessResult) -> None:
            if result.returncode != 0 or not result.cleanup_quiesced:
                return
            returncode, _stdout, stderr, quiesced = _sync_run_cleanup_command(
                [
                    "git",
                    "update-ref",
                    "-d",
                    f"refs/heads/{self._branch}",
                    self._base_commit or "",
                ],
                cwd=self._source,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
            )
            if returncode != 0 or not quiesced:
                raise OSError(
                    "cancelled branch claim could not be rolled back atomically: "
                    + stderr.decode(errors="replace").strip()
                )

        claimed = await self._run_git(
            "update-ref",
            f"refs/heads/{self._branch}",
            self._base_commit,
            "0" * 40,
            late_compensation=compensate_claim,
        )
        if claimed.returncode != 0:
            self._branch_preexisting = True
            raise RuntimeError(
                f"git worktree add failed: branch {self._branch} already exists"
            )
        self._branch_preexisting = False
        self._branch_cleanup_pending = True
        self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-wt-")
        self._worktree_add_attempted = True
        self._add_owner_active = True

        def compensate_add(_result: _ThreadProcessResult) -> None:
            self._late_compensate_worktree_add()

        added = await self._run_git(
            "worktree",
            "add",
            self._worktree_dir,
            self._branch,
            late_compensation=compensate_add,
        )
        self._add_owner_active = False
        if added.returncode != 0:
            detail = added.stderr.strip()
            await self._refresh_partial_worktree_ownership()
            raise RuntimeError(f"git worktree add failed: {detail}")
        self._worktree_registered = True
        self._branch_cleanup_pending = True

        self.workspace = self._worktree_dir
        self._local_env = LocalEnvironment(self._worktree_dir)
        return self._worktree_dir

    def _late_compensate_worktree_add(self) -> None:
        errors: list[str] = []
        try:
            worktree_dir = self._worktree_dir
            owned_branch = self._branch_cleanup_pending
            if worktree_dir:
                try:
                    returncode, stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "list", "--porcelain"],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    expected = os.path.realpath(worktree_dir)
                    registered = returncode == 0 and quiesced and expected in {
                        os.path.realpath(line.removeprefix("worktree "))
                        for line in stdout.decode(errors="replace").splitlines()
                        if line.startswith("worktree ")
                    }
                    if registered:
                        self._worktree_registered = True
                        owned_branch = self._branch_preexisting is False
                        self._branch_cleanup_pending = owned_branch
                    elif returncode != 0 or not quiesced:
                        errors.append("late worktree ownership probe was indeterminate")
                except BaseException as exc:
                    logger.error("late git worktree ownership probe failed: %s", exc)
                    errors.append(f"late worktree ownership probe failed: {exc}")
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    if returncode == 0 and quiesced:
                        self._worktree_registered = False
                    else:
                        self._worktree_registered = True
                        errors.append("late git worktree remove failed")
                except BaseException as exc:
                    logger.error("late git worktree compensation failed: %s", exc)
                    self._worktree_registered = True
                    errors.append(f"late git worktree remove failed: {exc}")
                if os.path.exists(worktree_dir):
                    shutil.rmtree(worktree_dir, ignore_errors=True)
                    if os.path.exists(worktree_dir):
                        errors.append(
                            f"late worktree directory still exists: {worktree_dir}"
                        )
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "prune", "--expire", "now"],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    if returncode != 0 or not quiesced:
                        errors.append("late git worktree prune failed")
                except BaseException as exc:
                    logger.error("late git worktree prune failed: %s", exc)
                    errors.append(f"late git worktree prune failed: {exc}")
            if owned_branch and self._branch_preexisting is False:
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        [
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/heads/{self._branch}",
                        ],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                except BaseException as exc:
                    logger.error("late git branch probe failed: %s", exc)
                    self._branch_cleanup_pending = True
                    errors.append(f"late git branch probe failed: {exc}")
                else:
                    if quiesced and returncode == 1:
                        self._branch_cleanup_pending = False
                    elif quiesced and returncode == 0:
                        try:
                            (
                                delete_returncode,
                                _stdout,
                                _stderr,
                                delete_quiesced,
                            ) = _sync_run_cleanup_command(
                                ["git", "branch", "-D", self._branch],
                                cwd=self._source,
                                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                                timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                            )
                        except BaseException as exc:
                            errors.append(f"late git branch delete failed: {exc}")
                            self._branch_cleanup_pending = True
                        else:
                            self._branch_cleanup_pending = not (
                                delete_quiesced and delete_returncode == 0
                            )
                            if self._branch_cleanup_pending:
                                errors.append("late git branch delete failed")
                    else:
                        self._branch_cleanup_pending = True
                        errors.append("late git branch probe was indeterminate")
        finally:
            self._add_owner_active = False
        if errors:
            raise OSError("; ".join(errors))

    async def _refresh_partial_worktree_ownership(self) -> None:
        if not self._worktree_dir:
            return
        listed = await self._run_git("worktree", "list", "--porcelain")
        if listed.returncode != 0:
            raise RuntimeError(
                f"cannot inspect worktree registration: {listed.stderr.strip()}"
            )
        if listed.stdout_truncated:
            raise RuntimeError("git worktree list output was truncated")
        expected = os.path.realpath(self._worktree_dir)
        registered_paths = {
            os.path.realpath(line.removeprefix("worktree "))
            for line in listed.stdout.splitlines()
            if line.startswith("worktree ")
        }
        self._worktree_registered = expected in registered_paths
        if self._worktree_registered:
            if self._branch_preexisting is False:
                self._branch_cleanup_pending = True

    async def _cleanup_worktree_resources(self, *, raise_on_error: bool) -> None:
        errors: list[str] = []
        if self._add_owner_active:
            error = "git worktree add owner is still performing compensation"
            if raise_on_error:
                raise OSError(error)
            logger.error(error)
            return
        worktree_dir = self._worktree_dir
        ownership_unknown = False
        if worktree_dir and self._worktree_add_attempted:
            try:
                await self._refresh_partial_worktree_ownership()
            except BaseException as exc:
                errors.append(f"worktree ownership probe failed: {exc}")
                ownership_unknown = True

        if worktree_dir and (self._worktree_registered or ownership_unknown):
            try:
                removed = await self._run_git(
                    "worktree",
                    "remove",
                    "--force",
                    worktree_dir,
                )
            except BaseException as exc:
                errors.append(f"git worktree remove failed: {exc}")
            else:
                if removed.returncode == 0:
                    self._worktree_registered = False
                    ownership_unknown = False
                else:
                    self._worktree_registered = True
                    errors.append(
                        "git worktree remove failed: "
                        + removed.stderr.strip()
                    )

        if worktree_dir and os.path.exists(worktree_dir):
            await _run_owned_blocking_io(
                shutil.rmtree,
                worktree_dir,
                True,
            )
            if os.path.exists(worktree_dir):
                errors.append(f"worktree directory still exists: {worktree_dir}")

        if worktree_dir and (self._worktree_registered or ownership_unknown):
            try:
                pruned = await self._run_git(
                    "worktree",
                    "prune",
                    "--expire",
                    "now",
                )
                if pruned.returncode != 0:
                    errors.append(
                        "git worktree prune failed: "
                        + pruned.stderr.strip()
                    )
                else:
                    listed = await self._run_git("worktree", "list", "--porcelain")
                    if listed.stdout_truncated:
                        raise RuntimeError("git worktree list output was truncated")
                    if listed.returncode == 0 and os.path.realpath(
                        worktree_dir
                    ) not in {
                        os.path.realpath(line.removeprefix("worktree "))
                        for line in listed.stdout.splitlines()
                        if line.startswith("worktree ")
                    }:
                        self._worktree_registered = False
                        ownership_unknown = False
            except BaseException as exc:
                errors.append(f"git worktree prune failed: {exc}")

        if self._branch_cleanup_pending:
            try:
                branch_probe = await self._run_git(
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{self._branch}",
                )
            except BaseException as exc:
                errors.append(f"branch cleanup probe failed: {exc}")
            else:
                if branch_probe.returncode == 1:
                    self._branch_cleanup_pending = False
                elif branch_probe.returncode != 0:
                    errors.append(
                        "branch cleanup probe failed: "
                        + branch_probe.stderr.strip()
                    )
                else:
                    try:
                        deleted = await self._run_git("branch", "-D", self._branch)
                    except BaseException as exc:
                        errors.append(f"git branch -D failed: {exc}")
                    else:
                        if deleted.returncode == 0:
                            self._branch_cleanup_pending = False
                        else:
                            errors.append(
                                "git branch -D failed: "
                                + deleted.stderr.strip()
                            )

        directory_gone = not worktree_dir or not os.path.exists(worktree_dir)
        if (
            not self._worktree_registered
            and not ownership_unknown
            and directory_gone
        ):
            self._worktree_dir = None
            self._local_env = None
        if (
            not self._branch_cleanup_pending
            and not self._worktree_registered
            and not ownership_unknown
            and directory_gone
        ):
            self._worktree_add_attempted = False
        if errors and raise_on_error:
            raise OSError("; ".join(errors))

    async def get_diff(self) -> str:
        """Get the diff of changes made in this worktree."""
        self._ensure_active()
        if not self._local_env:
            return ""
        if not self._base_commit:
            raise RuntimeError("worktree base commit is unavailable")
        # Intent-to-add makes untracked, non-ignored paths visible to git diff
        # without staging their contents. Comparing against the creation commit
        # also captures commits made by the child after the worktree was created.
        intent = await self._local_env.exec_cmd("git add --intent-to-add -- .")
        if intent.returncode != 0:
            raise RuntimeError(f"cannot enumerate untracked worktree files: {intent.stderr.strip()}")
        result = await self._local_env.exec_cmd(
            f"git diff --binary {shlex.quote(self._base_commit)} --"
        )
        if result.returncode != 0:
            raise RuntimeError(f"cannot collect worktree diff: {result.stderr.strip()}")
        if result.stdout_truncated:
            raise RuntimeError("worktree diff exceeded capture limit")
        return result.stdout

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.exec_cmd(cmd, timeout)

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        await self._local_env.write_file(path, content)

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.write_temp_file(
            content,
            prefix=prefix,
            suffix=suffix,
        )

    async def remove_file(self, path: str) -> None:
        if self._local_env is not None:
            await self._local_env.remove_file(path)

    async def abort(self) -> None:
        await super().abort()
        if self._local_env is not None:
            await self._local_env.abort()

    async def cleanup(self) -> None:
        """Remove worktree and temporary branch."""
        await _await_owned_transaction(
            self._cleanup_worktree_resources(raise_on_error=True),
            failure_note="worktree cleanup",
        )


class DockerEnvironment(Environment):
    """Docker container sandbox — for eval / SWE-bench.

    Two modes:
    - Start mode (default): ``setup()`` starts a fresh container, optionally
      mounting a local directory. Used by ``harness/evaluator.py``.
    - Attach mode: pass ``container_id`` to target an ALREADY-RUNNING container
      (e.g. an official ``sweb.eval`` image started outside this process). No
      ``setup()`` call is needed and ``cleanup()`` leaves the container alone.

    ``exec_workdir`` sets the ``docker exec -w`` working directory. ``command_prefix``
    wraps each command before execution (e.g. activating a conda env). When a prefix
    is supplied, commands run through a login shell (``bash -lc``) so the activation
    sticks. ``timeout_returncode`` is the ``returncode`` reported on timeout.

    Ref: design doc Environment abstraction + Harness Engineering.
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
            raise ValueError(
                "an attached Docker environment cannot own a backing environment"
            )
        self._image = _validate_docker_image_reference(image)
        self.workspace = workspace
        attached_reference = (
            _validate_docker_container_reference(container_id)
            if container_id is not None
            else None
        )
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
                raise RuntimeError(
                    "Could not bind attached Docker container to a full id"
                ) from exc
            if (
                result.returncode != 0
                or result.stdout_dropped_bytes
                or result.stderr_dropped_bytes
            ):
                detail = (result.stderr or result.stdout).decode(
                    "utf-8", errors="replace"
                ).strip()
                raise RuntimeError(
                    "Could not inspect attached Docker container: " + detail
                )
            full_id = self._parse_attached_container_inspect(
                result.stdout,
                expected_reference=reference,
            )
            if not full_id:
                raise RuntimeError(
                    "Attached Docker container identity was ambiguous or changed"
                )
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
            raise OSError(
                "docker setup compensation could not prove container ownership"
            )
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
        if not quiesced or (
            returncode != 0 and _DOCKER_MISSING_RE.search(stderr) is None
        ):
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
                f"Timed out starting Docker container after "
                f"{setup_timeout:g}s; cleanup_quiesced={exc.cleanup_quiesced}"
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
                + (
                    " (cleanup failed; container reference retained)"
                    if not removed
                    else ""
                )
            )

        if result.stdout_dropped_bytes:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: id output was truncated"
                + (
                    " (cleanup failed; container reference retained)"
                    if not removed
                    else ""
                )
            )
        self._container_id = self._container_id_from_output(result.stdout)
        if not self._container_id:
            removed = await self._force_remove_container(container_name)
            if not removed:
                self._aborted = True
            raise RuntimeError(
                "Failed to start container: docker returned no container id"
                + (
                    " (cleanup failed; container reference retained)"
                    if not removed
                    else ""
                )
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
                + (
                    " (cleanup failed; container reference retained)"
                    if not removed
                    else ""
                )
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
        if self._container_id and re.fullmatch(
            r"[0-9a-fA-F]{64}", self._container_id
        ) and self._container_id != inspected_id:
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
                logger.warning(
                    "Docker cleanup could not prove reference absent: %s", reference
                )
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
            f"Docker container {operation} failed for {container_ref}; "
            "container reference retained for recovery"
        )

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
            stopped = await _await_owned_operation(
                self._cleanup_container_exec(token)
            )
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
                raise OSError(
                    "container command descendants required forced cleanup"
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
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                raise OSError(
                    "timed out command could not be terminated inside attached container"
                )
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
                detail = (
                    "cancelled command could not be terminated inside attached container"
                )
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
                    add_note(
                        "container command cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            if not stopped:
                detail = "failed command could not be terminated inside container"
                logger.error(detail)
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise original

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        quoted_path = shlex.quote(path)
        result = await self.exec_cmd(f"cat -- {quoted_path}")
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        if result.stdout_truncated:
            raise OSError(
                f"docker read exceeded capture limit for {path}; "
                f"dropped {result.stdout_dropped_bytes} bytes"
            )
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
                raise OSError(
                    "timed out write could not be terminated inside attached container"
                ) from exc
            raise OSError(f"docker write timed out for {path}") from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            stopped = await self._cleanup_container_exec_or_revoke(token)
            if not stopped:
                detail = (
                    "cancelled write could not be terminated inside attached container"
                )
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
                    add_note(
                        "container write cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
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
        if (
            (result.returncode or 0) != 0
            or result.stdout_dropped_bytes
            or result.stderr_dropped_bytes
        ):
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise OSError(
                f"docker write failed for {path} "
                f"(exit {result.returncode}): {detail}"
            )
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
            if (
                created.returncode != 0
                or created.stdout_truncated
                or created.stderr_truncated
            ):
                raise OSError("failed to create exclusive container temporary file")
            await self.write_file(path, content)
        except BaseException as original:
            try:
                await _await_owned_operation(self.remove_file(path))
            except BaseException as cleanup_exc:
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(
                        "container temporary file cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            raise
        return path

    async def remove_file(self, path: str) -> None:
        result = await self.exec_cmd(
            f"rm -f -- {shlex.quote(path)}",
            timeout=DOCKER_WRITE_TIMEOUT_SECONDS,
        )
        if (
            result.returncode != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise OSError(f"failed to remove container temporary file: {path}")

    @staticmethod
    def _raise_teardown_failures(
        operation: str,
        failures: list[tuple[str, BaseException]],
    ) -> None:
        if not failures:
            return
        first_stage, first_failure = failures[0]
        add_note = getattr(first_failure, "add_note", None)
        if callable(add_note):
            add_note(f"Docker {operation} failed during {first_stage}")
            for stage, failure in failures[1:]:
                add_note(
                    f"additional Docker {operation} failure during {stage}: "
                    f"{type(failure).__name__}: {failure}"
                )
        raise first_failure

    async def abort(self) -> None:
        await super().abort()
        failures: list[tuple[str, BaseException]] = []
        if not self._attached:
            try:
                await self._remove_owned_container_or_raise(operation="abort")
            except BaseException as exc:
                failures.append(("container removal", exc))
        backing = self._backing_environment
        if backing is not None:
            try:
                await _await_owned_operation(backing.abort())
            except BaseException as exc:
                failures.append(("backing abort", exc))
            try:
                await _await_owned_operation(backing.cleanup())
            except BaseException as exc:
                failures.append(("backing cleanup", exc))
            else:
                self._backing_environment = None
        self._raise_teardown_failures("abort", failures)

    async def cleanup(self) -> None:
        if self._attached:
            return
        failures: list[tuple[str, BaseException]] = []
        try:
            await self._remove_owned_container_or_raise(operation="cleanup")
        except BaseException as exc:
            failures.append(("container removal", exc))
        backing = self._backing_environment
        if backing is not None:
            try:
                await _await_owned_operation(backing.cleanup())
            except BaseException as exc:
                failures.append(("backing cleanup", exc))
            else:
                self._backing_environment = None
        self._raise_teardown_failures("cleanup", failures)
