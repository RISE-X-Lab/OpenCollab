"""Small asyncio subprocess supervisor shared by environment adapters."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.adapters._env_base import ExecResult
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note

PROCESS_OUTPUT_CAPTURE_BYTES = 1024 * 1024
PROCESS_TERM_GRACE_SECONDS = 0.05
PROCESS_KILL_GRACE_SECONDS = 2.0
_BACKGROUND_SPAWN_CLEANUPS: set[asyncio.Task[None]] = set()


class ProcessCleanupError(RuntimeError):
    """A subprocess group remained alive after bounded cleanup."""


class ProcessTimeout(asyncio.TimeoutError):
    """A command was killed at its deadline, carrying what it had written.

    A timed-out command used to reach the model as an empty result and one
    sentence saying it timed out. Everything the command had already printed --
    which for a test run is the failures it got through before it hung, and for
    a build is where it stopped -- was read into the capture buffers, then
    discarded when the reader tasks were cancelled. The model was left to guess
    whether the command had done nothing or nearly everything, and its only
    move was to run it again.

    Subclasses ``asyncio.TimeoutError`` so every existing ``except`` clause
    still catches it; a caller that wants the partial output reads ``partial``.
    """

    def __init__(self, message: str, partial: ProcessResult | None = None):
        super().__init__(message)
        self.partial = partial


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0

    def to_exec_result(self) -> ExecResult:
        """Decode captured process output into the public environment result."""
        return ExecResult(
            self.returncode,
            self.stdout.decode("utf-8", errors="replace"),
            self.stderr.decode("utf-8", errors="replace"),
            self.stdout_dropped_bytes > 0,
            self.stderr_dropped_bytes > 0,
            self.stdout_dropped_bytes,
            self.stderr_dropped_bytes,
        )


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[bytes, int]:
    if stream is None:
        return b"", 0
    head_limit = (limit + 1) // 2
    tail_limit = limit - head_limit
    head = bytearray()
    tail = bytearray()
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        available = max(0, head_limit - len(head))
        head.extend(chunk[:available])
        remainder = chunk[available:]
        if tail_limit and remainder:
            tail.extend(remainder)
            if len(tail) > tail_limit:
                del tail[:-tail_limit]
    retained = bytes(head + tail)
    return retained, max(0, total - len(retained))


def _read_proc_stat(pid: str) -> tuple[int, int, int, bytes]:
    with open(f"/proc/{pid}/stat", "rb") as stat_file:
        value = stat_file.read()
    name_start = value.index(b"(")
    name_end = value.rindex(b")")
    fields = value[name_end + 1 :].split()
    # comm can contain spaces, parentheses and arbitrary bytes. The remaining
    # fields start at field 3 (state), with pgrp at 5 and starttime at 22.
    if name_end <= name_start or len(fields) < 20 or len(fields[0]) != 1:
        raise ValueError("incomplete process stat")
    return int(value[:name_start]), int(fields[2]), int(fields[19]), fields[0]


def _proc_has_complete_visibility() -> bool:
    """Require an unrestricted proc mount in our own PID namespace."""
    try:
        pid, group_id, _started, _state = _read_proc_stat("self")
        if pid != os.getpid() or group_id != os.getpgrp():
            return False
        with open("/proc/self/status", encoding="utf-8") as status:
            namespace_ids = [line.split()[1:] for line in status if line.startswith("NStgid:")]
        # Equal numeric PIDs alone can coincide across nested namespaces.
        if namespace_ids != [[str(pid)]]:
            return False
        with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
            proc_mounts = 0
            for line in mounts:
                mount, filesystem = line.split(" - ", 1)
                fields = mount.split()
                fs_fields = filesystem.split()
                mountpoint = fields[4]
                if mountpoint.startswith("/proc/"):
                    component = mountpoint.removeprefix("/proc/").split("/", 1)[0]
                    if component.isdecimal() or component in {"self", "thread-self"}:
                        return False
                if mountpoint != "/proc":
                    continue
                proc_mounts += 1
                if fields[3] != "/" or fs_fields[0] != "proc":
                    return False
                options = fields[5].split(",") + fs_fields[2].split(",")
                if any(
                    option.startswith("hidepid=") and option not in {"hidepid=0", "hidepid=off"}
                    for option in options
                ):
                    return False
            return proc_mounts == 1
    except (OSError, ValueError, IndexError, UnicodeError):
        return False


def _exited_proc_group_members(group_id: int) -> frozenset[tuple[int, int, int]] | None:
    """Return exited identities after a complete scan, or an unknown/live result."""
    if not _proc_has_complete_visibility():
        return None
    members: set[tuple[int, int, int]] = set()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdecimal():
                continue
            try:
                pid, member_group, started, state = _read_proc_stat(entry)
            except (FileNotFoundError, ProcessLookupError):
                # A missing stat can also belong to a partial/overlaid proc view.
                # Only a vanished process directory proves an enumeration race.
                try:
                    os.stat(f"/proc/{entry}")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                return None
            if pid != int(entry):
                return None
            if member_group != group_id:
                continue
            if state not in {b"Z", b"X", b"x"}:
                return None
            # A thread-group leader can become a zombie via pthread_exit while
            # its other threads keep running. Require a complete singleton task
            # directory before treating that leader as an exited process.
            if os.listdir(f"/proc/{entry}/task") != [entry]:
                return None
            members.add((pid, started, member_group))
    except (OSError, ValueError, IndexError):
        return None
    return frozenset(members)


def _group_exists(group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "linux":
        members = _exited_proc_group_members(group_id)
        if members and members == _exited_proc_group_members(group_id):
            # Once every enumerated member is exited it cannot fork. A second
            # complete scan catches children forked before the first stat read,
            # and comparing start times also catches PID reuse or group changes.
            return False
    return True


async def _wait_for_group_exit(group_id: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _group_exists(group_id):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    term_grace: float = PROCESS_TERM_GRACE_SECONDS,
    kill_grace: float = PROCESS_KILL_GRACE_SECONDS,
) -> bool:
    """Stop one subprocess and its POSIX process group within fixed bounds."""
    group_id = process.pid

    def send(sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(group_id, sig)
            elif process.returncode is None:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    if not await _wait_for_group_exit(group_id, term_grace):
        send(signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=kill_grace)
    except asyncio.TimeoutError:
        return False
    return await _wait_for_group_exit(group_id, kill_grace)


async def _require_process_exit(
    process: asyncio.subprocess.Process,
    message: str,
    cause: BaseException | None = None,
    *,
    propagate: bool = True,
) -> None:
    quiesced = await await_owned_operation(
        terminate_process(process),
        propagate_cancellation=propagate,
    )
    if not quiesced:
        raise ProcessCleanupError(message) from cause


class ProcessRegistry:
    """Track commands owned by one environment for abort and cleanup."""

    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._revoked = False
        self._handoffs = 0
        self._condition = asyncio.Condition()

    async def spawn(
        self,
        factory: Callable[[], Awaitable[asyncio.subprocess.Process]],
    ) -> asyncio.subprocess.Process:
        async with self._condition:
            if self._revoked:
                raise RuntimeError("process registry has been revoked")
            self._handoffs += 1
        try:
            process = await _spawn_owned(factory)
            try:
                async with self._condition:
                    revoked = self._revoked
                    if not revoked:
                        self._processes.add(process)
            except asyncio.CancelledError as cancellation:
                if not await await_owned_operation(terminate_process(process)):
                    raise ProcessCleanupError(
                        "cancelled subprocess registration did not quiesce"
                    ) from None
                raise cancellation
            if revoked:
                if not await await_owned_operation(
                    terminate_process(process),
                    propagate_cancellation=True,
                ):
                    raise ProcessCleanupError("revoked subprocess spawn did not quiesce")
                raise RuntimeError("process registry has been revoked")
            return process
        finally:
            async with self._condition:
                self._handoffs -= 1
                self._condition.notify_all()

    def discard(self, process: asyncio.subprocess.Process) -> None:
        self._processes.discard(process)

    async def _abort_owned(self) -> None:
        async with self._condition:
            self._revoked = True
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._handoffs == 0),
                    timeout=PROCESS_KILL_GRACE_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise ProcessCleanupError(
                    "subprocess spawn handoff did not quiesce"
                ) from exc
            processes = tuple(self._processes)
        results = await asyncio.gather(
            *(terminate_process(process) for process in processes),
            return_exceptions=True,
        )
        for process, result in zip(processes, results, strict=True):
            if result is True:
                self._processes.discard(process)
        failures = [result for result in results if result is not True]
        if failures:
            failure = ProcessCleanupError(
                "one or more subprocess groups did not quiesce"
            )
            for result in failures:
                detail = (
                    f"{type(result).__name__}: {result}"
                    if isinstance(result, BaseException)
                    else "termination returned an unproven result"
                )
                add_exception_note(failure, f"subprocess cleanup failure: {detail}")
            cause = next(
                (result for result in failures if isinstance(result, BaseException)),
                None,
            )
            if cause is not None:
                raise failure from cause
            raise failure

    async def abort(self) -> None:
        await await_owned_operation(self._abort_owned(), propagate_cancellation=True)


async def _spawn_owned(
    factory: Callable[[], Awaitable[asyncio.subprocess.Process]],
) -> asyncio.subprocess.Process:
    """Finish spawn handoff before propagating caller cancellation."""
    owner = asyncio.create_task(factory())
    try:
        return await asyncio.shield(owner)
    except asyncio.CancelledError as cancellation:
        process = await await_owned_operation(owner)
        if not await await_owned_operation(terminate_process(process)):
            raise ProcessCleanupError(
                "cancelled subprocess spawn did not quiesce"
            ) from None
        raise cancellation


def _retain_spawn_cleanup(task: asyncio.Task[None]) -> None:
    """Keep a late spawn reaper alive and consume its terminal result."""
    _BACKGROUND_SPAWN_CLEANUPS.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _BACKGROUND_SPAWN_CLEANUPS.discard(completed)
        try:
            completed.result()
        except BaseException:
            pass

    task.add_done_callback(finish)


async def _reap_late_spawn(
    spawn_owner: asyncio.Task[asyncio.subprocess.Process],
    registry: ProcessRegistry | None,
) -> None:
    """Terminate a process that appears after its caller stopped waiting."""
    try:
        process = await spawn_owner
    except BaseException:
        return
    await _require_process_exit(
        process,
        "late subprocess spawn did not quiesce",
        propagate=False,
    )
    if registry is not None:
        registry.discard(process)


async def _spawn_before_deadline(
    spawn_awaitable: Awaitable[asyncio.subprocess.Process],
    *,
    deadline: float,
    registry: ProcessRegistry | None,
) -> asyncio.subprocess.Process:
    """Bound the spawn handoff while preserving ownership of late processes."""
    spawn_owner = asyncio.create_task(spawn_awaitable)
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        done, _pending = await asyncio.wait({spawn_owner}, timeout=remaining)
    except asyncio.CancelledError:
        spawn_owner.cancel()
        _retain_spawn_cleanup(
            asyncio.create_task(_reap_late_spawn(spawn_owner, registry))
        )
        raise
    if spawn_owner not in done:
        spawn_owner.cancel()
        _retain_spawn_cleanup(
            asyncio.create_task(_reap_late_spawn(spawn_owner, registry))
        )
        raise asyncio.TimeoutError("subprocess spawn exceeded command timeout")
    return spawn_owner.result()


def timed_out_result(
    exc: BaseException, returncode: int, timeout: float
) -> ExecResult:
    """A timeout the model can act on: the notice plus what was already written.

    The notice goes at the top of stderr rather than replacing it, so a reader
    scanning for why a command failed still finds it first.
    """
    notice = f"Command timed out after {timeout:g}s"
    partial = getattr(exc, "partial", None)
    if partial is None:
        return ExecResult(returncode, "", notice, timed_out=True)
    result = partial.to_exec_result()
    stderr = f"{notice}\n{result.stderr}" if result.stderr else notice
    return ExecResult(
        returncode,
        result.stdout,
        stderr,
        result.stdout_truncated,
        result.stderr_truncated,
        result.stdout_dropped_bytes,
        result.stderr_dropped_bytes,
        timed_out=True,
    )


async def _drain_partial_output(
    stdout_task: asyncio.Task,
    stderr_task: asyncio.Task,
    *,
    returncode: int | None,
) -> ProcessResult | None:
    """What the killed command had written, or ``None`` if it cannot be read.

    The process is already gone by the time this runs, so both readers are at
    EOF and the gather returns at once; the grace period is there for the case
    where a pipe is still being drained. Returning ``None`` rather than raising
    keeps a failure here from replacing the timeout the caller is reporting.
    """
    try:
        (stdout, stdout_dropped), (stderr, stderr_dropped) = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=PROCESS_KILL_GRACE_SECONDS,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError, OSError, ValueError):
        return None
    return ProcessResult(
        returncode=returncode if returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        stdout_dropped_bytes=stdout_dropped,
        stderr_dropped_bytes=stderr_dropped,
    )


async def run_process(
    command: str | Sequence[str],
    *,
    shell: bool,
    cwd: str | None = None,
    timeout: float,
    registry: ProcessRegistry | None = None,
    input_bytes: bytes | None = None,
    output_limit: int = PROCESS_OUTPUT_CAPTURE_BYTES,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run one bounded command and prove cleanup on timeout or cancellation."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    if not isinstance(output_limit, int) or isinstance(output_limit, bool) or output_limit < 0:
        raise ValueError("output_limit must be a non-negative integer")
    timeout_seconds = float(timeout)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    process_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": asyncio.subprocess.PIPE if input_bytes is not None else None,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        process_kwargs["start_new_session"] = True

    async def spawn() -> asyncio.subprocess.Process:
        if shell:
            if not isinstance(command, str):
                raise TypeError("shell commands must be text")
            return await asyncio.create_subprocess_shell(command, **process_kwargs)
        if isinstance(command, str):
            raise TypeError("exec commands must be a sequence")
        return await asyncio.create_subprocess_exec(*command, **process_kwargs)

    process = await _spawn_before_deadline(
        registry.spawn(spawn) if registry is not None else _spawn_owned(spawn),
        deadline=deadline,
        registry=registry,
    )

    stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit))
    quiesced = False

    async def write_input_and_wait() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write(input_bytes or b"")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        await process.wait()

    try:
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(write_input_and_wait(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            await _require_process_exit(process, "timed out command did not quiesce", exc)
            quiesced = True
            raise ProcessTimeout(
                f"command exceeded its {timeout_seconds:g}s deadline",
                await _drain_partial_output(
                    stdout_task, stderr_task, returncode=process.returncode
                ),
            ) from exc
        except asyncio.CancelledError as cancellation:
            await _require_process_exit(
                process, "cancelled command did not quiesce", propagate=False
            )
            quiesced = True
            raise cancellation
        await _require_process_exit(process, "command process group did not quiesce after leader exit")
        quiesced = True
        try:
            (stdout, stdout_dropped), (stderr, stderr_dropped) = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=PROCESS_KILL_GRACE_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await _require_process_exit(
                process, "command output pipes and process group did not quiesce", exc
            )
            quiesced = True
            raise ProcessCleanupError("command output pipes did not quiesce") from exc
        return ProcessResult(
            returncode=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            stdout_dropped_bytes=stdout_dropped,
            stderr_dropped_bytes=stderr_dropped,
        )
    finally:
        if registry is not None and quiesced:
            registry.discard(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await await_owned_operation(
            asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
            propagate_cancellation=True,
        )


__all__ = [
    "PROCESS_OUTPUT_CAPTURE_BYTES",
    "ProcessCleanupError",
    "ProcessTimeout",
    "timed_out_result",
    "ProcessRegistry",
    "ProcessResult",
    "run_process",
    "terminate_process",
]
