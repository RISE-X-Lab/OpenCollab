"""Small asyncio subprocess supervisor shared by environment adapters."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.adapters._env_base import ExecResult
from opencollab.application.async_timeout import await_owned_operation

PROCESS_OUTPUT_CAPTURE_BYTES = 1024 * 1024
PROCESS_TERM_GRACE_SECONDS = 0.05
PROCESS_KILL_GRACE_SECONDS = 2.0


class ProcessCleanupError(RuntimeError):
    """A subprocess group remained alive after bounded cleanup."""


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
    retained = bytearray()
    dropped = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        available = max(0, limit - len(retained))
        kept = chunk[:available]
        retained.extend(kept)
        dropped += len(chunk) - len(kept)
    return bytes(retained), dropped


def _group_exists(group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
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
            await self._condition.wait_for(lambda: self._handoffs == 0)
            processes = tuple(self._processes)
        results = await asyncio.gather(
            *(terminate_process(process) for process in processes),
            return_exceptions=True,
        )
        for process, result in zip(processes, results, strict=True):
            if result is True:
                self._processes.discard(process)
        if any(result is not True for result in results):
            raise ProcessCleanupError("one or more subprocess groups did not quiesce")

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

    process = await (registry.spawn(spawn) if registry is not None else _spawn_owned(spawn))

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
            await asyncio.wait_for(write_input_and_wait(), timeout=float(timeout))
        except asyncio.TimeoutError as exc:
            await _require_process_exit(process, "timed out command did not quiesce", exc)
            quiesced = True
            raise
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
    "ProcessRegistry",
    "ProcessResult",
    "run_process",
    "terminate_process",
]
