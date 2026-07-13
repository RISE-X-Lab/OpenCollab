"""Owned evaluator process lifecycle for the per-instance SWE-bench runner."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _runner():
    module = sys.modules.get("scripts.run_swebench_eval_per_instance")
    if module is not None:
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("run_swebench_eval_per_instance.py"):
        return module
    raise RuntimeError("per-instance evaluator runner module is unavailable")


class EvaluatorSpawnTimeout(TimeoutError):
    pass


def _write_helper_status(fd: int, payload: dict) -> None:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("evaluator helper status write made no progress")
        view = view[written:]


def _evaluator_helper_main(
    write_fd: int,
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    log_fd: int,
    deadline: float,
) -> None:
    try:
        os.setsid()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_DFL)
        _write_helper_status(
            write_fd,
            {"status": "helper_ready", "pgid": os.getpid()},
        )
        try:
            process = _runner()._EVALUATOR_POPEN(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
            )
        except BaseException as exc:
            _write_helper_status(
                write_fd,
                {
                    "status": "spawn_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        else:
            _write_helper_status(
                write_fd,
                {"status": "spawned", "pid": int(process.pid)},
            )
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _write_helper_status(write_fd, {"status": "timeout"})
            except BaseException as exc:
                _write_helper_status(
                    write_fd,
                    {
                        "status": "worker_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            else:
                _write_helper_status(
                    write_fd,
                    {"status": "completed", "returncode": int(returncode)},
                )
        while True:
            signal.pause()
    except BaseException:
        os._exit(_runner().PROCESS_CLEANUP_FAILED_EXIT_CODE)


class OwnedEvaluatorProcess:
    """Popen-like handle whose pid owns a recoverable helper process group."""

    def __init__(
        self,
        *,
        pid: int,
        read_fd: int,
        buffer: bytearray,
        deadline: float,
    ) -> None:
        self.pid = pid
        self._read_fd = read_fd
        self._buffer = buffer
        self._deadline = deadline
        self._cleanup_started = False
        self._returncode: int | None = None
        self._reaped = False

    def begin_cleanup(self) -> None:
        self._cleanup_started = True

    def _close_status(self) -> None:
        if self._read_fd >= 0:
            os.close(self._read_fd)
            self._read_fd = -1

    def _wait_reaped(self, deadline: float) -> int:
        while True:
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._reaped = True
                self._close_status()
                return self._returncode or 0
            if waited == self.pid:
                self._reaped = True
                self._close_status()
                if self._returncode is not None:
                    return self._returncode
                decoder = getattr(os, "waitstatus_to_exitcode", None)
                if callable(decoder):
                    return decoder(status)
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return -os.WTERMSIG(status)
                raise RuntimeError("evaluator helper returned an unsupported wait status")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("evaluator-helper", 0)
            time.sleep(min(0.01, remaining))

    def _message(self, deadline: float) -> dict | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OSError("evaluator helper returned malformed status") from exc
                if not isinstance(value, dict):
                    raise OSError("evaluator helper returned non-object status")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _writable, _exceptional = select.select(
                [self._read_fd],
                [],
                [],
                min(0.05, remaining),
            )
            if not readable:
                continue
            chunk = os.read(self._read_fd, 4096)
            if not chunk:
                return None
            self._buffer.extend(chunk)
            if len(self._buffer) > 64 * 1024:
                raise OSError("evaluator helper status exceeded its byte bound")

    def wait(self, timeout: float | None = None) -> int:
        if self._cleanup_started:
            requested_deadline = (
                time.monotonic() + max(0.0, timeout)
                if timeout is not None
                else time.monotonic() + _runner().PROCESS_KILL_REAP_TIMEOUT_SECONDS
            )
            return self._wait_reaped(requested_deadline)
        requested_deadline = self._deadline
        if timeout is not None:
            requested_deadline = min(
                requested_deadline,
                time.monotonic() + max(0.0, timeout),
            )
        if self._returncode is not None:
            return self._returncode
        while True:
            message = self._message(requested_deadline)
            if message is None:
                raise subprocess.TimeoutExpired("evaluator", timeout)
            status = message.get("status")
            if status == "completed":
                self._returncode = int(message.get("returncode", 125))
                return self._returncode
            if status == "timeout":
                raise subprocess.TimeoutExpired("evaluator", timeout)
            if status == "worker_error":
                raise OSError(str(message.get("error") or "evaluator helper failed"))

    def terminate(self) -> None:
        self.begin_cleanup()
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        self.begin_cleanup()
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cleanup_raw_helper(pid: int, *, ready: bool) -> bool:
    gentle_deadline = time.monotonic() + 0.05
    while time.monotonic() < gentle_deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            if not ready or not _runner()._process_group_exists(pid):
                return True
            break
        if waited == pid:
            if not ready or not _runner()._process_group_exists(pid):
                return True
            break
        time.sleep(0.005)
    try:
        if ready:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + _runner().PROCESS_KILL_REAP_TIMEOUT_SECONDS
    reaped = False
    while time.monotonic() < deadline:
        if not reaped:
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = True
            else:
                reaped = waited == pid
        group_gone = not ready or not _runner()._process_group_exists(pid)
        if reaped and group_gone:
            return True
        time.sleep(0.01)
    return reaped and (not ready or not _runner()._process_group_exists(pid))


def _spawn_owned_evaluator(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_fd: int,
    wall_timeout: float,
    spawn_timeout: float = _runner().PROCESS_SPAWN_TIMEOUT_SECONDS,
) -> OwnedEvaluatorProcess:
    started_at = time.monotonic()
    deadline = started_at + wall_timeout
    read_fd = -1
    write_fd = -1
    try:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
    except BaseException:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        raise
    if pid == 0:
        os.close(read_fd)
        _evaluator_helper_main(
            write_fd,
            cmd=cmd,
            cwd=cwd,
            env=env,
            log_fd=log_fd,
            deadline=deadline,
        )
        os._exit(_runner().PROCESS_CLEANUP_FAILED_EXIT_CODE)
    os.close(write_fd)
    process = OwnedEvaluatorProcess(
        pid=pid,
        read_fd=read_fd,
        buffer=bytearray(),
        deadline=deadline,
    )
    ready = False
    try:
        spawn_deadline = min(deadline, started_at + spawn_timeout)
        while True:
            message = process._message(spawn_deadline)
            if message is None:
                raise EvaluatorSpawnTimeout("evaluator Popen exceeded its spawn bound")
            status = message.get("status")
            if status == "helper_ready":
                ready = True
                continue
            if status == "spawned":
                return process
            if status == "spawn_error":
                raise OSError(str(message.get("error") or "evaluator failed to start"))
            raise OSError(f"unexpected evaluator helper status: {status}")
    except BaseException as exc:
        process._close_status()
        cleanup_ok = _cleanup_raw_helper(pid, ready=ready)
        if not cleanup_ok:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(f"evaluator spawn helper {pid} did not quiesce after SIGKILL")
        raise


def _wait_for_owned_cleanup(
    done: threading.Event,
    *,
    timeout: float,
) -> tuple[bool, BaseException | None]:
    """Wait for cleanup while deferring repeated caller interrupts."""
    deadline = time.monotonic() + max(0.0, timeout)
    interruption: BaseException | None = None
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            done.wait(min(0.05, remaining))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
    return done.is_set(), interruption


def _consume_process_exit(process: subprocess.Popen) -> None:
    try:
        process.wait()
    except BaseException:
        pass


def _schedule_process_exit_consumer(process: subprocess.Popen) -> None:
    threading.Thread(
        target=_consume_process_exit,
        args=(process,),
        name=f"swe-eval-reap-{getattr(process, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def _process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(pgid: int, *, deadline: float) -> bool:
    while _runner()._process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _proc_process_start_identity(pid: int) -> str:
    path = Path("/proc") / str(pid) / "stat"
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return ""
    try:
        raw = os.read(fd, 8193)
        if len(raw) > 8192:
            return ""
    finally:
        os.close(fd)
    close = raw.rfind(b")")
    if close < 0:
        return ""
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        return ""
    try:
        int(fields[19])
    except ValueError:
        return ""
    return "proc:" + fields[19].decode("ascii", errors="strict")


def _identity_helper_main(write_fd: int, pid: int, deadline: float) -> None:
    try:
        os.setsid()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_DFL)
        _write_helper_status(write_fd, {"status": "helper_ready"})
        try:
            process = _runner()._PROCESS_IDENTITY_POPEN(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _stderr = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            _write_helper_status(
                write_fd,
                {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:2_048],
                },
            )
        else:
            value = stdout.strip() if process.returncode == 0 else ""
            _write_helper_status(
                write_fd,
                {"status": "result", "value": value[:1_024]},
            )
    except BaseException:
        os._exit(_runner().PROCESS_CLEANUP_FAILED_EXIT_CODE)
    os._exit(0)


def _read_identity_helper_message(
    fd: int,
    buffer: bytearray,
    deadline: float,
) -> dict | None:
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        readable, _writable, _exceptional = select.select(
            [fd],
            [],
            [],
            min(0.05, remaining),
        )
        if not readable:
            continue
        chunk = os.read(fd, 2048)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > 4096:
            return None


def process_start_identity(pid: int) -> str:
    if Path("/proc").is_dir():
        return _proc_process_start_identity(pid)
    if os.name != "posix":
        return ""
    deadline = time.monotonic() + _runner().PROCESS_IDENTITY_TIMEOUT_SECONDS
    read_fd = -1
    write_fd = -1
    try:
        read_fd, write_fd = os.pipe()
        helper_pid = os.fork()
    except OSError:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        return ""
    if helper_pid == 0:
        os.close(read_fd)
        _identity_helper_main(write_fd, pid, deadline)
        os._exit(_runner().PROCESS_CLEANUP_FAILED_EXIT_CODE)
    os.close(write_fd)
    ready = False
    result = ""
    buffer = bytearray()
    try:
        while True:
            message = _read_identity_helper_message(read_fd, buffer, deadline)
            if message is None:
                break
            if message.get("status") == "helper_ready":
                ready = True
                continue
            if message.get("status") == "result":
                result = str(message.get("value") or "")
            break
    finally:
        os.close(read_fd)
        _cleanup_raw_helper(helper_pid, ready=ready)
    return result


def _claim_residual_group_is_live(claim: dict) -> bool:
    try:
        pgid = int(claim.get("evaluator_pgid") or 0)
    except (TypeError, ValueError):
        return False
    if pgid <= 1 or not _runner()._process_group_exists(pgid):
        return False
    expected_start = str(claim.get("evaluator_start_identity") or "")
    current_start = _runner().process_start_identity(pgid)
    if expected_start and current_start and expected_start != current_start:
        return False
    return True


def _terminate_process_group_owned(
    process: subprocess.Popen,
    *,
    term_timeout: float,
    kill_timeout: float,
) -> tuple[bool, list[str]]:
    messages: list[str] = []
    pgid = process.pid
    begin_cleanup = getattr(process, "begin_cleanup", None)
    if callable(begin_cleanup):
        begin_cleanup()
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            _schedule_process_exit_consumer(process)
            return False, ["technical cleanup failure: permission denied terminating process"]

    leader_reaped = False
    try:
        process.wait(timeout=max(0.0, term_deadline - time.monotonic()))
        leader_reaped = True
    except ChildProcessError:
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass

    group_gone = _wait_for_process_group_exit(pgid, deadline=term_deadline)
    if leader_reaped and group_gone:
        return True, messages
    if leader_reaped:
        messages.append("process-group descendants remained after leader exit; sending SIGKILL")
    else:
        messages.append("process did not terminate after SIGTERM; sending SIGKILL")

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    if not leader_reaped:
        try:
            process.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
            leader_reaped = True
        except ChildProcessError:
            leader_reaped = True
        except subprocess.TimeoutExpired:
            pass
    group_gone = _wait_for_process_group_exit(pgid, deadline=kill_deadline)
    if not leader_reaped:
        messages.append(f"technical cleanup failure: process was not reaped within {kill_timeout:g}s after SIGKILL")
        _schedule_process_exit_consumer(process)
    if not group_gone:
        messages.append(f"technical cleanup failure: process group remained within {kill_timeout:g}s after SIGKILL")
    return leader_reaped and group_gone, messages


def terminate_process_group(
    process: subprocess.Popen,
    log_file,
    *,
    term_timeout: float = _runner().PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = _runner().PROCESS_KILL_REAP_TIMEOUT_SECONDS,
) -> bool:
    """Terminate and reap a child with bounded, interrupt-resistant cleanup."""
    state: dict[str, object] = {}
    done = threading.Event()

    def cleanup() -> None:
        try:
            state["result"] = _terminate_process_group_owned(
                process,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
            )
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    cleanup_thread = threading.Thread(
        target=cleanup,
        name=f"swe-eval-cleanup-{getattr(process, 'pid', 'unknown')}",
        daemon=True,
    )
    cleanup_thread.start()
    completed, interruption = _runner()._wait_for_owned_cleanup(
        done,
        timeout=term_timeout + kill_timeout + _runner().PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
    )
    if completed and "result" in state:
        reaped, messages = state["result"]
    else:
        reaped = False
        messages = [
            "technical cleanup failure: process cleanup exceeded its outer bound; background cleanup remains attached"
        ]
        if "error" in state:
            messages.append(f"cleanup raised {type(state['error']).__name__}: {state['error']}")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except (ProcessLookupError, PermissionError):
                pass
    for message in messages:
        log_file.write(message + "\n")
    if interruption is not None:
        raise interruption
    return bool(reaped)


def ensure_process_group_quiesced_after_wait(
    process: subprocess.Popen,
    log_file,
) -> bool:
    """Prove that a normally reaped leader left no owned descendants behind."""
    if not _runner()._process_group_exists(process.pid):
        return True
    log_file.write(
        "evaluator leader exited while process-group descendants remained; terminating residual process group\n"
    )
    if isinstance(process, OwnedEvaluatorProcess):
        return _runner().terminate_process_group(
            process,
            log_file,
            term_timeout=_runner().HELPER_RESIDUAL_TERM_GRACE_SECONDS,
            kill_timeout=_runner().PROCESS_KILL_REAP_TIMEOUT_SECONDS,
        )
    return _runner().terminate_process_group(process, log_file)


class ActiveProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    def add(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(process)

    def discard(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def terminate_all(self, log_file) -> bool:
        with self._lock:
            processes = tuple(self._processes)
        if not processes:
            return True

        remaining = len(processes)
        remaining_lock = threading.Lock()
        done = threading.Event()
        outcomes: list[bool] = []

        def terminate_one(process: subprocess.Popen) -> None:
            nonlocal remaining
            try:
                outcomes.append(_runner().terminate_process_group(process, log_file))
            except BaseException:
                outcomes.append(False)
            finally:
                self.discard(process)
                with remaining_lock:
                    remaining -= 1
                    if remaining == 0:
                        done.set()

        for process in processes:
            threading.Thread(
                target=terminate_one,
                args=(process,),
                name=f"swe-eval-stop-{getattr(process, 'pid', 'unknown')}",
                daemon=True,
            ).start()
        completed, interruption = _runner()._wait_for_owned_cleanup(
            done,
            timeout=(
                _runner().PROCESS_TERM_GRACE_SECONDS
                + _runner().PROCESS_KILL_REAP_TIMEOUT_SECONDS
                + _runner().PROCESS_CLEANUP_OUTER_SLACK_SECONDS
                + 1.0
            ),
        )
        if interruption is not None:
            raise interruption
        return completed and len(outcomes) == len(processes) and all(outcomes)
