"""Owned subprocess execution and bounded output capture."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable

from opencollab.adapters._env_config import (
    _PROCESS_POPEN,
    PROCESS_IO_JOIN_TIMEOUT_SECONDS,
    PROCESS_KILL_REAP_TIMEOUT_SECONDS,
    PROCESS_OUTPUT_CAPTURE_BYTES,
    PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
    PROCESS_TERM_GRACE_SECONDS,
)
from opencollab.adapters._env_file_io import (
    _await_owned_transaction,
    _positive_finite_timeout,
)
from opencollab.adapters.retirement_registry import INTERNAL_RETIREMENT_LOG_ENV
from opencollab.application.exception_notes import add_exception_note

logger = logging.getLogger(__name__)


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
            marker = f"\n...[opencollab truncated {dropped} bytes]...\n".encode()
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
    cwd_fd: int | None = None,
    timeout: object,
    timeout_name: str,
) -> tuple[int, bytes, bytes, bool]:
    timeout_seconds = _positive_finite_timeout(timeout, name=timeout_name)
    owner = _ThreadProcessOwner(
        command,
        shell=False,
        cwd=cwd,
        cwd_fd=cwd_fd,
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
        cwd_fd: int | None,
        timeout: float,
        input_data: bytes | None,
        late_compensation: Callable[["_ThreadProcessResult"], None] | None,
    ):
        self._command = command
        self._shell = shell
        self._cwd = cwd
        self._cwd_fd = cwd_fd
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
            popen_kwargs = {
                "shell": self._shell,
                "cwd": self._cwd,
                "stdin": subprocess.PIPE if self._input_data is not None else subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "start_new_session": True,
            }
            if INTERNAL_RETIREMENT_LOG_ENV in os.environ:
                child_env = os.environ.copy()
                child_env.pop(INTERNAL_RETIREMENT_LOG_ENV, None)
                popen_kwargs["env"] = child_env
            command = self._command
            if self._cwd_fd is not None:
                if self._shell:
                    if not isinstance(command, str):
                        raise ValueError("shell process command must be text")
                    wrapper = (
                        "import os,sys; "
                        "fd=int(sys.argv[1]); command=sys.argv[2]; "
                        "os.fchdir(fd); os.close(fd); "
                        "os.execv('/bin/sh', ['/bin/sh', '-c', command])"
                    )
                    command = [sys.executable, "-c", wrapper, str(self._cwd_fd), command]
                else:
                    if not isinstance(command, list) or not command:
                        raise ValueError("descriptor-pinned process command must be a non-empty list")
                    wrapper = (
                        "import os,sys; "
                        "fd=int(sys.argv[1]); command=sys.argv[2:]; "
                        "os.fchdir(fd); os.close(fd); "
                        "os.execvp(command[0], command)"
                    )
                    command = [sys.executable, "-c", wrapper, str(self._cwd_fd), *command]
                popen_kwargs["shell"] = False
                popen_kwargs["cwd"] = None
                popen_kwargs["pass_fds"] = (self._cwd_fd,)
            proc = _PROCESS_POPEN(command, **popen_kwargs)
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
                self.result.error = OSError("process leader exited while descendants remained alive")
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
            lingering_io = (writer is not None and writer.is_alive()) or any(reader.is_alive() for reader in readers)
            if self.result.error is None and io_errors:
                self.result.error = io_errors[0]
            if self.result.error is None and lingering_io:
                self.result.error = OSError("subprocess pipes did not reach EOF within the drain bound")
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
    cwd_fd: int | None = None,
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
        cwd_fd=cwd_fd,
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
            completed = await _await_owned_operation(_wait_thread_event(owner.finished, timeout=cancel_bound))
        except BaseException as exc:
            completed = False
            add_exception_note(
                original,
                f"process owner wait failed: {type(exc).__name__}: {exc}",
            )
        if not completed:
            add_exception_note(
                original,
                "process owner continues cleanup in a non-daemon thread",
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
                add_exception_note(
                    original,
                    f"process compensation observation failed: {type(exc).__name__}: {exc}",
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
        if completed and compensation_completed and owner.result.compensation_error is not None:
            compensation_error = owner.result.compensation_error
            add_exception_note(
                original,
                f"process compensation failed: {type(compensation_error).__name__}: {compensation_error}",
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
            completed = await _await_owned_operation(_wait_thread_event(owner.finished, timeout=cancel_bound))
        except BaseException as exc:
            completed = False
            add_exception_note(
                original,
                f"process owner wait failed: {type(exc).__name__}: {exc}",
            )
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
                add_exception_note(
                    original,
                    f"process compensation observation failed: {type(exc).__name__}: {exc}",
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
        if completed and compensation_completed and owner.result.compensation_error is not None:
            compensation_error = owner.result.compensation_error
            add_exception_note(
                original,
                f"process compensation failed: {type(compensation_error).__name__}: {compensation_error}",
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
            add_exception_note(
                not_quiesced,
                f"original process error: {type(result.error).__name__}: {result.error}",
            )
            raise not_quiesced from result.error
        if result.compensation_error is not None:
            add_exception_note(
                result.error,
                "process compensation failed: "
                f"{type(result.compensation_error).__name__}: "
                f"{result.compensation_error}",
            )
        raise result.error
    if result.timed_out:
        if result.compensation_error is not None:
            logger.error(
                "process timeout compensation failed: %s",
                result.compensation_error,
            )
        raise _OwnedProcessTimeout(cleanup_quiesced=(result.cleanup_quiesced and result.compensation_error is None))
    if not result.cleanup_quiesced or result.returncode is None:
        raise _OwnedProcessNotQuiesced(
            "process leader or descendants did not quiesce; execution result is indeterminate",
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


async def _await_owned_operation(
    awaitable,
    *,
    propagate_cancellation: bool = False,
):
    """Finish teardown while choosing who owns cancellation propagation.

    Compensation paths already retain an outer exception, so repeated caller
    cancellation must not replace it.  Public lifecycle boundaries opt in to
    propagation after their owned operation has quiesced.
    """
    if propagate_cancellation:
        return await _await_owned_transaction(
            awaitable,
            failure_note="owned teardown operation",
        )

    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            continue
