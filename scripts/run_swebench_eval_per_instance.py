#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness import swe_eval_records as swe_records  # noqa: E402
from opencollab.harness.swe_eval_records import (  # noqa: E402
    MAX_JSON_DOCUMENT_BYTES,
    RecordInputFormatError,
    RecordInputLimitError,
    UnsafeRecordInputError,
    SUBMISSION_INTEGRITY_PROVEN,
    embedded_workflow_metric,
    is_completed_prediction,
    metric_submission_integrity,
)
from opencollab.adapters.safe_files import (  # noqa: E402
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    regular_path_identity,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)

_PROCESS_IDENTITY_POPEN = subprocess.Popen
_ORIGINAL_EVALUATOR_POPEN = subprocess.Popen
_EVALUATOR_POPEN = subprocess.Popen
PROCESS_TERM_GRACE_SECONDS = 30.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
PROCESS_CLEANUP_FAILED_EXIT_CODE = 125
PROCESS_SPAWN_TIMEOUT_SECONDS = 10.0
HELPER_RESIDUAL_TERM_GRACE_SECONDS = 0.1
PROCESS_IDENTITY_TIMEOUT_SECONDS = 5.0
MAX_PATH_IDENTITY_BYTES = 240
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_LINE_BYTES = 16 * 1024 * 1024
MAX_DATASET_ROWS = 10_000
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
TECHNICAL_REPORT_STATUSES = {
    "technical_eval_failed",
    "eval_failed",
    "eval_start_failed",
    "eval_driver_error",
    "empty_eval_patch_invalid",
    "blocked_missing_eval_deps",
    "blocked_missing_eval_image",
    "blocked_missing_eval_spec",
}


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
            process = _EVALUATOR_POPEN(
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
                returncode = process.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
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
        os._exit(PROCESS_CLEANUP_FAILED_EXIT_CODE)


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
                return os.waitstatus_to_exitcode(status)
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
                else time.monotonic() + PROCESS_KILL_REAP_TIMEOUT_SECONDS
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
            if not ready or not _process_group_exists(pid):
                return True
            break
        if waited == pid:
            if not ready or not _process_group_exists(pid):
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
    deadline = time.monotonic() + PROCESS_KILL_REAP_TIMEOUT_SECONDS
    reaped = False
    while time.monotonic() < deadline:
        if not reaped:
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = True
            else:
                reaped = waited == pid
        group_gone = not ready or not _process_group_exists(pid)
        if reaped and group_gone:
            return True
        time.sleep(0.01)
    return reaped and (not ready or not _process_group_exists(pid))


def _spawn_owned_evaluator(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_fd: int,
    wall_timeout: float,
    spawn_timeout: float = PROCESS_SPAWN_TIMEOUT_SECONDS,
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
        os._exit(PROCESS_CLEANUP_FAILED_EXIT_CODE)
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
                add_note(
                    f"evaluator spawn helper {pid} did not quiesce after SIGKILL"
                )
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
    while _process_group_exists(pgid):
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
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
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
            process = _PROCESS_IDENTITY_POPEN(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _stderr = process.communicate(
                timeout=max(0.0, deadline - time.monotonic())
            )
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
        os._exit(PROCESS_CLEANUP_FAILED_EXIT_CODE)
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
    deadline = time.monotonic() + PROCESS_IDENTITY_TIMEOUT_SECONDS
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
        os._exit(PROCESS_CLEANUP_FAILED_EXIT_CODE)
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
    if pgid <= 1 or not _process_group_exists(pgid):
        return False
    expected_start = str(claim.get("evaluator_start_identity") or "")
    current_start = process_start_identity(pgid)
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
        messages.append(
            "process-group descendants remained after leader exit; sending SIGKILL"
        )
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
        messages.append(
            f"technical cleanup failure: process was not reaped within {kill_timeout:g}s after SIGKILL"
        )
        _schedule_process_exit_consumer(process)
    if not group_gone:
        messages.append(
            f"technical cleanup failure: process group remained within {kill_timeout:g}s after SIGKILL"
        )
    return leader_reaped and group_gone, messages


def terminate_process_group(
    process: subprocess.Popen,
    log_file,
    *,
    term_timeout: float = PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = PROCESS_KILL_REAP_TIMEOUT_SECONDS,
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
    completed, interruption = _wait_for_owned_cleanup(
        done,
        timeout=term_timeout + kill_timeout + PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
    )
    if completed and "result" in state:
        reaped, messages = state["result"]
    else:
        reaped = False
        messages = [
            "technical cleanup failure: process cleanup exceeded its outer bound; "
            "background cleanup remains attached"
        ]
        if "error" in state:
            messages.append(
                f"cleanup raised {type(state['error']).__name__}: {state['error']}"
            )
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
    if not _process_group_exists(process.pid):
        return True
    log_file.write(
        "evaluator leader exited while process-group descendants remained; "
        "terminating residual process group\n"
    )
    if isinstance(process, OwnedEvaluatorProcess):
        return terminate_process_group(
            process,
            log_file,
            term_timeout=HELPER_RESIDUAL_TERM_GRACE_SECONDS,
            kill_timeout=PROCESS_KILL_REAP_TIMEOUT_SECONDS,
        )
    return terminate_process_group(process, log_file)


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
                outcomes.append(terminate_process_group(process, log_file))
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
        completed, interruption = _wait_for_owned_cleanup(
            done,
            timeout=(
                PROCESS_TERM_GRACE_SECONDS
                + PROCESS_KILL_REAP_TIMEOUT_SECONDS
                + PROCESS_CLEANUP_OUTER_SLACK_SECONDS
                + 1.0
            ),
        )
        if interruption is not None:
            raise interruption
        return completed and len(outcomes) == len(processes) and all(outcomes)


def positive_timeout_seconds(value: object, *, name: str) -> float:
    raw = str(value).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}")
    return timeout


def positive_int_arg(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("expected a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def nonnegative_int_arg(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return int(value)


def validate_path_identity(value: object, *, name: str) -> str:
    text = str(value)
    if not text:
        raise ValueError(f"{name} must not be empty")
    if text in {".", ".."}:
        raise ValueError(f"{name} must not be a dot path segment")
    if "/" in text or "\\" in text:
        raise ValueError(f"{name} must not contain path separators")
    windows_path = PureWindowsPath(text)
    if Path(text).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{name} must not be absolute or drive-qualified")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise ValueError(
                f"{name} must not contain control, format, or surrogate characters"
            )
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PATH_IDENTITY_BYTES:
        raise ValueError(
            f"{name} exceeds {MAX_PATH_IDENTITY_BYTES} UTF-8 bytes"
        )
    return text


def validate_model_identity(value: object) -> str:
    name = "model_name_or_path"
    text = str(value)
    if not text:
        raise ValueError(f"{name} must not be empty")
    if "\\" in text:
        raise ValueError(f"{name} must not contain backslash separators")
    windows_path = PureWindowsPath(text)
    if Path(text).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{name} must not be absolute or drive-qualified")
    segments = text.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} must not contain empty or dot path segments")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise ValueError(
                f"{name} must not contain control, format, or surrogate characters"
            )
    encoded_component = text.replace("/", "__").encode("utf-8")
    if len(encoded_component) > MAX_PATH_IDENTITY_BYTES:
        raise ValueError(
            f"{name} exceeds {MAX_PATH_IDENTITY_BYTES} encoded UTF-8 bytes"
        )
    return text


def read_jsonl(path: Path) -> list[dict]:
    try:
        payload = read_regular_bytes(
            path,
            max_bytes=swe_records.MAX_JSONL_SCAN_BYTES,
        )
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise RecordInputLimitError(
            "JSONL input exceeds "
            f"{swe_records.MAX_JSONL_SCAN_BYTES} bytes: {path}"
        ) from exc
    except OSError as exc:
        raise UnsafeRecordInputError(
            f"cannot safely read JSONL input: {path}"
        ) from exc
    rows: list[dict] = []
    retained_bytes = 0
    for line in payload.splitlines(keepends=True):
        if not line.strip():
            continue
        if len(line) > swe_records.MAX_JSONL_LINE_BYTES:
            raise RecordInputLimitError(
                "JSONL line exceeds "
                f"{swe_records.MAX_JSONL_LINE_BYTES} bytes: {path}"
            )
        retained_bytes += len(line)
        if (
            len(rows) >= swe_records.MAX_JSONL_RETAINED_ROWS
            or retained_bytes > swe_records.MAX_JSONL_RETAINED_BYTES
        ):
            raise RecordInputLimitError(
                f"JSONL input exceeds retained row or byte limit: {path}"
            )
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordInputFormatError(f"invalid JSONL record in {path}") from exc
        if not isinstance(value, dict):
            raise RecordInputFormatError(f"JSONL record must be an object: {path}")
        rows.append(value)
    return rows


def read_dataset(path: Path) -> list[dict]:
    try:
        raw = read_regular_bytes(path, max_bytes=MAX_DATASET_BYTES)
    except ValueError as exc:
        raise ValueError(f"dataset exceeds {MAX_DATASET_BYTES} bytes: {path}") from exc
    except OSError as exc:
        raise ValueError(f"dataset is not a bounded regular file: {path}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        payload = None
    else:
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            if len(payload) > MAX_DATASET_ROWS:
                raise ValueError(f"dataset exceeds {MAX_DATASET_ROWS} rows")
            if not all(isinstance(row, dict) for row in payload):
                raise ValueError("dataset JSON list must contain only objects")
            return payload
        raise ValueError("dataset must be a JSON object, JSON list, or JSONL records")

    rows: list[dict] = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        if len(line) > MAX_DATASET_LINE_BYTES:
            raise ValueError(
                f"dataset line exceeds {MAX_DATASET_LINE_BYTES} bytes: {path}"
            )
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"dataset contains invalid JSONL: {path}") from exc
        if not isinstance(row, dict):
            raise ValueError("dataset JSONL must contain only objects")
        rows.append(row)
        if len(rows) > MAX_DATASET_ROWS:
            raise ValueError(f"dataset exceeds {MAX_DATASET_ROWS} rows")
    return rows


def report_path(work_dir: Path, run_id: str, model_name: str, instance_id: str) -> Path:
    run_id = validate_path_identity(run_id, name="run_id")
    model_name = validate_model_identity(model_name)
    instance_id = validate_path_identity(instance_id, name="instance_id")
    return (
        work_dir
        / "logs"
        / "run_evaluation"
        / run_id
        / model_name.replace("/", "__")
        / instance_id
        / "report.json"
    )


def prediction_identity(prediction: dict) -> dict:
    patch = str(prediction.get("model_patch") or "")
    return {
        "instance_id": str(prediction.get("instance_id") or ""),
        "record_id": str(prediction.get("record_id") or ""),
        "patch_sha256": hashlib.sha256(
            patch.encode("utf-8", errors="surrogatepass")
        ).hexdigest() if patch else "",
    }


def prediction_is_eval_eligible(prediction: dict) -> bool:
    patch = str(prediction.get("model_patch") or "")
    if not patch.strip():
        return False
    modern_keys = {
        "workflow_metric",
        "workflow_status",
        "runner_returncode",
        "patch_sha256",
        "patch_sha",
        "model_patch_sha256",
        "submission_eligible",
        "execution_quiesced",
        "patch_extraction_succeeded",
    }
    if modern_keys.intersection(prediction):
        metric = embedded_workflow_metric(prediction)
        return (
            is_completed_prediction(prediction)
            and metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_PROVEN
        )
    # Rows predating embedded workflow metrics remain evaluable when they are
    # complete plain prediction records.  Any modern provenance signal above
    # switches the row to the strict paired-integrity gate.
    return True


def identity_path(path: Path) -> Path:
    return path.with_name("opencollab-attempt.json")


def _read_bounded_json_safe(
    path: Path,
    *,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
) -> tuple[object, os.stat_result] | None:
    target = Path(os.path.abspath(os.fspath(path)))
    try:
        parent_fd = _open_directory_no_symlinks(target.parent)
    except OSError:
        return None
    fd = -1
    try:
        try:
            before = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(target.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            return None
        current = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
        if identity(opened) != identity(after) or identity(after) != identity(current):
            return None
        if not _directory_path_matches_fd(target.parent, parent_fd):
            return None
        try:
            return json.loads(payload.decode("utf-8")), after
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def file_fingerprint(path: Path) -> str:
    try:
        _dev, inode, size, mtime_ns, ctime_ns = regular_path_identity(path)
    except OSError:
        return ""
    return f"{mtime_ns}:{ctime_ns}:{size}:{inode}"


def _stat_fingerprint(opened: os.stat_result) -> str:
    return f"{opened.st_mtime_ns}:{opened.st_ctime_ns}:{opened.st_size}:{opened.st_ino}"


def _fsync_directory(path: Path) -> None:
    fd = _open_directory_no_symlinks(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_exclusive_lock(fd: int, *, label: str) -> None:
    deadline = time.monotonic() + HARNESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out acquiring {label} after "
                f"{HARNESS_LOCK_TIMEOUT_SECONDS:g}s"
            )
        time.sleep(min(0.01, remaining))


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    path = Path(os.path.abspath(os.fspath(path)))
    ensure_directory_no_symlinks(path.parent)
    safe_flags = (
        flags
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        parent_fd = _open_directory_no_symlinks(path.parent)
        try:
            try:
                before = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise OSError(f"refusing non-regular harness file: {path}")
            try:
                if before is None:
                    fd = os.open(
                        path.name,
                        safe_flags | os.O_CREAT | os.O_EXCL,
                        mode,
                        dir_fd=parent_fd,
                    )
                    created = True
                else:
                    fd = os.open(path.name, safe_flags, dir_fd=parent_fd)
                    created = False
            except (FileExistsError, FileNotFoundError):
                continue
            try:
                opened = os.fstat(fd)
                current = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                    raise OSError(f"refusing non-regular harness file: {path}")
                opened_identity = (opened.st_dev, opened.st_ino)
                if (current.st_dev, current.st_ino) != opened_identity:
                    continue
                if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                    continue
                if not _directory_path_matches_fd(path.parent, parent_fd):
                    break
                if created:
                    os.fsync(parent_fd)
                result_fd = fd
                fd = -1
                return result_fd, created
            except FileNotFoundError:
                pass
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(parent_fd)
    raise OSError(f"harness file did not stabilize while opening: {path}")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    write_regular_bytes_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    _write_bytes_atomic(path, encoded)


def _unlink_durable(path: Path) -> None:
    unlink_regular_file_durable(path)


def _open_append_text(path: Path):
    fd, _created = _open_regular_file(path, os.O_WRONLY | os.O_APPEND, 0o644)
    return os.fdopen(fd, "a", encoding="utf-8")


def write_identity(
    path: Path,
    identity: dict,
    *,
    status: str = "started",
    pid: int = 0,
    started_at_ns: int | None = None,
    prior_report_fingerprint: str | None = None,
) -> dict:
    if started_at_ns is None:
        started_at_ns = time.time_ns()
    if prior_report_fingerprint is None:
        prior_report_fingerprint = file_fingerprint(path.with_name("report.json"))
    payload = {
        "schema": "opencollab.swe_eval_attempt.v1",
        **identity,
        "started_at_ns": started_at_ns,
        "status": status,
        "pid": pid,
        "prior_report_fingerprint": prior_report_fingerprint,
    }
    _write_json_atomic(path, payload)
    return payload


def _pid_is_active(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_path(work_dir: Path, instance_id: str) -> Path:
    instance_id = validate_path_identity(instance_id, name="instance_id")
    stem = hashlib.sha256(instance_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return work_dir / ".opencollab" / "claims" / f"{stem}.json"


def acquire_claim(
    work_dir: Path,
    instance_id: str,
    identity: dict,
    *,
    lease_seconds: int,
    owner_token: str,
) -> tuple[bool, Path]:
    path = _claim_path(work_dir, instance_id)
    ensure_directory_no_symlinks(path.parent)
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _read_bounded_json_safe(path)
        existing = document[0] if document is not None else {}
        if isinstance(existing, dict):
            now_ns = time.time_ns()
            try:
                lease_until_ns = int(existing.get("lease_until_ns") or 0)
            except (TypeError, ValueError):
                lease_until_ns = 0
            residual_status = existing.get("status") in {
                "running",
                "cleanup_failed",
            }
            if residual_status and _claim_residual_group_is_live(existing):
                existing["lease_until_ns"] = now_ns + 60_000_000_000
                existing["residual_checked_at_ns"] = now_ns
                _write_json_atomic(path, existing)
                return False, path
            if (
                existing.get("status") != "cleanup_failed"
                and lease_until_ns > now_ns
            ):
                return False, path
        claimed_at_ns = time.time_ns()
        _write_json_atomic(
            path,
            {
                "schema": "opencollab.swe_eval_claim.v1",
                **identity,
                "owner_token": owner_token,
                "pid": os.getpid(),
                "status": "claimed",
                "claimed_at_ns": claimed_at_ns,
                "lease_until_ns": claimed_at_ns + max(1, lease_seconds) * 1_000_000_000,
            },
        )
        return True, path
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def update_claim_process(
    path: Path,
    *,
    owner_token: str,
    evaluator_pgid: int,
    evaluator_start_identity: str,
    status: str,
    lease_seconds: int,
) -> bool:
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _read_bounded_json_safe(path)
        if document is None:
            return False
        existing, _opened_stat = document
        if not isinstance(existing, dict) or existing.get("owner_token") != owner_token:
            return False
        now_ns = time.time_ns()
        existing.update(
            {
                "status": status,
                "evaluator_pgid": evaluator_pgid,
                "evaluator_start_identity": evaluator_start_identity,
                "updated_at_ns": now_ns,
                "lease_until_ns": now_ns
                + max(1, lease_seconds) * 1_000_000_000,
            }
        )
        _write_json_atomic(path, existing)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def release_claim(path: Path, *, owner_token: str) -> bool:
    lock_path = path.with_suffix(".lock")
    lock_fd, _created = _open_regular_file(lock_path, os.O_RDWR, 0o600)
    locked = False
    try:
        _acquire_exclusive_lock(lock_fd, label=f"claim lock {lock_path}")
        locked = True
        document = _read_bounded_json_safe(path)
        if document is None:
            return False
        existing, _opened_stat = document
        if not isinstance(existing, dict) or existing.get("owner_token") != owner_token:
            return False
        _unlink_durable(path)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def candidate_predictions_path(work_dir: Path, identity: dict) -> Path:
    instance_id = validate_path_identity(
        identity.get("instance_id") or "",
        name="instance_id",
    )
    key = "\0".join(
        [
            instance_id,
            str(identity.get("record_id") or ""),
            str(identity.get("patch_sha256") or ""),
        ]
    )
    stem = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
    return work_dir / ".opencollab" / "candidates" / f"{stem}.jsonl"


def write_candidate_prediction(work_dir: Path, prediction: dict, identity: dict) -> Path:
    path = candidate_predictions_path(work_dir, identity)
    payload = (json.dumps(prediction, ensure_ascii=False) + "\n").encode("utf-8")
    _write_bytes_atomic(path, payload)
    return path


def report_is_done(path: Path, instance_id: str, expected_identity: dict) -> bool:
    document = _read_bounded_json_safe(path)
    if document is None:
        return False
    data, report_stat = document
    item = data.get(instance_id) if isinstance(data, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("resolved"), bool):
        return False
    if str(item.get("status") or "") in TECHNICAL_REPORT_STATUSES or bool(
        item.get("error")
    ):
        return False
    embedded_sha = str(
        item.get("patch_sha256") or item.get("patch_sha") or item.get("model_patch_sha256") or ""
    )
    if embedded_sha:
        return embedded_sha == expected_identity.get("patch_sha256")
    sidecar = identity_path(path)
    sidecar_document = _read_bounded_json_safe(sidecar)
    if sidecar_document is None:
        return False
    attempt, _sidecar_stat = sidecar_document
    report_mtime_ns = report_stat.st_mtime_ns
    current_report_fingerprint = _stat_fingerprint(report_stat)
    if not isinstance(attempt, dict) or attempt.get("schema") != "opencollab.swe_eval_attempt.v1":
        return False
    if attempt.get("status") not in {"launching", "started", "completed"}:
        return False
    if str(attempt.get("instance_id") or "") != instance_id:
        return False
    if str(attempt.get("record_id") or "") != str(expected_identity.get("record_id") or ""):
        return False
    if str(attempt.get("patch_sha256") or "") != str(expected_identity.get("patch_sha256") or ""):
        return False
    if "prior_report_fingerprint" in attempt:
        return current_report_fingerprint != str(
            attempt.get("prior_report_fingerprint") or ""
        )
    try:
        return report_mtime_ns >= int(attempt.get("started_at_ns") or 0) > 0
    except (TypeError, ValueError):
        return False


def load_eval_queue(
    dataset_path: Path,
    predictions_path: Path,
    run_id: str,
    work_dir: Path,
) -> list[tuple[str, str, dict, dict]]:
    run_id = validate_path_identity(run_id, name="run_id")
    dataset = read_dataset(dataset_path)
    predictions = {
        str(row["instance_id"]): row
        for row in read_jsonl(predictions_path)
        if row.get("instance_id")
    }
    queue: list[tuple[str, str, dict, dict]] = []
    for instance in dataset:
        iid = str(instance.get("instance_id") or "")
        if not iid:
            continue
        iid = validate_path_identity(iid, name="instance_id")
        prediction = predictions.get(iid)
        if not prediction:
            continue
        if not prediction_is_eval_eligible(prediction):
            continue
        model_name = str(prediction.get("model_name_or_path") or "unknown-model")
        model_name = validate_model_identity(model_name)
        identity = prediction_identity(prediction)
        if report_is_done(report_path(work_dir, run_id, model_name, iid), iid, identity):
            continue
        queue.append((iid, model_name, identity, prediction))
    return queue


def run_one(
    *,
    iid: str,
    model_name: str,
    identity: dict,
    prediction: dict,
    ordinal: int,
    total: int,
    dataset_path: Path,
    work_dir: Path,
    run_id: str,
    timeout: int,
    namespace: str,
    cache_level: str,
    clean: str,
    outer_timeout: int,
    env: dict[str, str],
    print_lock: threading.Lock,
    active_processes: ActiveProcessRegistry | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[str, int]:
    iid = validate_path_identity(iid, name="instance_id")
    model_name = validate_model_identity(model_name)
    run_id = validate_path_identity(run_id, name="run_id")
    identity_iid = validate_path_identity(
        identity.get("instance_id") or "",
        name="identity.instance_id",
    )
    prediction_iid = validate_path_identity(
        prediction.get("instance_id") or "",
        name="prediction.instance_id",
    )
    prediction_model = validate_model_identity(
        prediction.get("model_name_or_path") or "unknown-model"
    )
    if identity_iid != iid or prediction_iid != iid:
        raise ValueError("instance_id does not match prediction identity")
    if prediction_model != model_name:
        raise ValueError("model_name_or_path does not match prediction")
    official_report = report_path(work_dir, run_id, model_name, iid)
    if report_is_done(official_report, iid, identity):
        with print_lock:
            print(f"[{ordinal}/{total}] skipping {iid} (report exists)", flush=True)
        return iid, 0

    log_path = work_dir / "command_logs" / f"{iid}.log"
    ensure_directory_no_symlinks(log_path.parent)
    report_dir = work_dir / "reports" / iid
    candidate_path = write_candidate_prediction(work_dir, prediction, identity)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_swebench_eval_with_docker_timeout.py"),
        "-d",
        str(dataset_path),
        "-s",
        "test",
        "-i",
        iid,
        "-p",
        str(candidate_path),
        "--max_workers",
        "1",
        "-t",
        str(timeout),
        "--cache_level",
        cache_level,
        "--clean",
        clean,
        "-id",
        run_id,
        "-n",
        namespace,
        "--report_dir",
        str(report_dir),
    ]
    owner_token = uuid.uuid4().hex
    acquired, claim_path = acquire_claim(
        work_dir,
        iid,
        identity,
        lease_seconds=outer_timeout + 60,
        owner_token=owner_token,
    )
    if not acquired:
        with print_lock:
            print(f"[{ordinal}/{total}] skipping {iid} (evaluation already claimed)", flush=True)
        return iid, 0
    release_owned_claim = True

    def retain_residual_claim(pgid: int, start_identity: str) -> None:
        nonlocal release_owned_claim
        release_owned_claim = False
        update_claim_process(
            claim_path,
            owner_token=owner_token,
            evaluator_pgid=pgid,
            evaluator_start_identity=start_identity,
            status="cleanup_failed",
            lease_seconds=max(300, outer_timeout + 60),
        )

    with print_lock:
        print(f"[{ordinal}/{total}] evaluating {iid}", flush=True)
    started_at_ns = time.time_ns()
    prior_report_fingerprint = file_fingerprint(official_report)
    attempt_path = identity_path(official_report)
    try:
        if stop_event is not None and stop_event.is_set():
            return iid, 130
        write_identity(
            attempt_path,
            identity,
            status="launching",
            pid=os.getpid(),
            started_at_ns=started_at_ns,
            prior_report_fingerprint=prior_report_fingerprint,
        )
        with _open_append_text(log_path) as log_file:
            log_file.write("\n\n$ " + " ".join(cmd) + "\n")
            log_file.write(f"# outer_timeout={outer_timeout}s\n")
            try:
                if (
                    os.name == "posix"
                    and subprocess.Popen is _ORIGINAL_EVALUATOR_POPEN
                ):
                    process = _spawn_owned_evaluator(
                        cmd,
                        cwd=work_dir,
                        env=env,
                        log_fd=log_file.fileno(),
                        wall_timeout=outer_timeout,
                    )
                else:
                    process = subprocess.Popen(
                        cmd,
                        cwd=work_dir,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except EvaluatorSpawnTimeout as exc:
                log_file.write(f"evaluator spawn timed out: {exc}\n")
                write_identity(
                    attempt_path,
                    identity,
                    status="failed_to_start",
                    started_at_ns=started_at_ns,
                    prior_report_fingerprint=prior_report_fingerprint,
                )
                return iid, PROCESS_CLEANUP_FAILED_EXIT_CODE
            except OSError as exc:
                log_file.write(f"failed to start evaluator: {exc}\n")
                write_identity(
                    attempt_path,
                    identity,
                    status="failed_to_start",
                    started_at_ns=started_at_ns,
                    prior_report_fingerprint=prior_report_fingerprint,
                )
                return iid, 127
            evaluator_start_identity = ""
            try:
                if active_processes is not None:
                    active_processes.add(process)
                evaluator_start_identity = process_start_identity(process.pid)
                claim_updated = update_claim_process(
                    claim_path,
                    owner_token=owner_token,
                    evaluator_pgid=process.pid,
                    evaluator_start_identity=evaluator_start_identity,
                    status="running",
                    lease_seconds=outer_timeout + 60,
                )
                if not claim_updated:
                    log_file.write(
                        "failed to persist evaluator process identity in claim\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    return iid, (
                        4
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                if stop_event is not None and stop_event.is_set():
                    log_file.write(
                        "evaluation stop requested after evaluator start; "
                        "terminating process group\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                        try:
                            write_identity(
                                attempt_path,
                                identity,
                                status="cleanup_failed",
                                pid=process.pid,
                                started_at_ns=started_at_ns,
                                prior_report_fingerprint=prior_report_fingerprint,
                            )
                        except Exception:
                            pass
                    return iid, (
                        130
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                try:
                    write_identity(
                        attempt_path,
                        identity,
                        status="started",
                        pid=process.pid,
                        started_at_ns=started_at_ns,
                        prior_report_fingerprint=prior_report_fingerprint,
                    )
                except Exception as exc:
                    log_file.write(f"failed to persist started evaluator identity: {exc}\n")
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    try:
                        write_identity(
                            attempt_path,
                            identity,
                            status="identity_persist_failed",
                            started_at_ns=started_at_ns,
                            prior_report_fingerprint=prior_report_fingerprint,
                        )
                    except Exception:
                        pass
                    return iid, (
                        4 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                try:
                    returncode = process.wait(timeout=outer_timeout)
                    cleanup_quiesced = ensure_process_group_quiesced_after_wait(
                        process,
                        log_file,
                    )
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                        returncode = PROCESS_CLEANUP_FAILED_EXIT_CODE
                except subprocess.TimeoutExpired:
                    log_file.write(
                        f"\nouter timeout after {outer_timeout}s; terminating process group\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    returncode = (
                        124
                        if cleanup_quiesced
                        else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                except Exception as exc:
                    log_file.write(
                        f"evaluator wait failed: {type(exc).__name__}: {exc}\n"
                    )
                    cleanup_quiesced = terminate_process_group(process, log_file)
                    if not cleanup_quiesced:
                        retain_residual_claim(
                            process.pid,
                            evaluator_start_identity,
                        )
                    returncode = (
                        4 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                if (
                    stop_event is not None
                    and stop_event.is_set()
                    and _process_group_exists(process.pid)
                ):
                    log_file.write(
                        "stop requested and evaluator descendants remain active; "
                        "retaining claim\n"
                    )
                    retain_residual_claim(
                        process.pid,
                        evaluator_start_identity,
                    )
                    returncode = PROCESS_CLEANUP_FAILED_EXIT_CODE
            except BaseException:
                cleanup_quiesced = False
                try:
                    cleanup_quiesced = terminate_process_group(process, log_file)
                except BaseException:
                    pass
                if not cleanup_quiesced:
                    retain_residual_claim(
                        process.pid,
                        evaluator_start_identity,
                    )
                    try:
                        write_identity(
                            attempt_path,
                            identity,
                            status="cleanup_failed",
                            pid=process.pid,
                            started_at_ns=started_at_ns,
                            prior_report_fingerprint=prior_report_fingerprint,
                        )
                    except Exception:
                        pass
                raise
            finally:
                if active_processes is not None:
                    active_processes.discard(process)
        if returncode == 0 and not report_is_done(official_report, iid, identity):
            returncode = 3
            with _open_append_text(log_path) as log_file:
                log_file.write("evaluator exited 0 without an exact-candidate report\n")
        if returncode == PROCESS_CLEANUP_FAILED_EXIT_CODE:
            final_status = "cleanup_failed"
            final_pid = process.pid
        else:
            final_status = "completed" if returncode == 0 else "failed"
            final_pid = 0
        try:
            write_identity(
                attempt_path,
                identity,
                status=final_status,
                pid=final_pid,
                started_at_ns=started_at_ns,
                prior_report_fingerprint=prior_report_fingerprint,
            )
        except Exception as exc:
            _unlink_durable(attempt_path)
            with _open_append_text(log_path) as log_file:
                log_file.write(
                    f"failed to persist evaluator final identity: "
                    f"{type(exc).__name__}: {exc}\n"
                )
            returncode = 4
    finally:
        if release_owned_claim:
            release_claim(claim_path, owner_token=owner_token)
    if returncode == 0:
        with print_lock:
            print(f"done {iid}", flush=True)
    else:
        with print_lock:
            print(f"failed {iid} exit={returncode}; see {log_path}", flush=True)
    return iid, returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SWE-bench official evaluation one instance at a time")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=positive_int_arg, default=1800)
    parser.add_argument("--limit", type=nonnegative_int_arg, default=0)
    parser.add_argument("--namespace", default="swebench")
    parser.add_argument("--cache-level", default="instance")
    parser.add_argument("--clean", default="False")
    parser.add_argument("--workers", type=positive_int_arg, default=1)
    parser.add_argument(
        "--outer-timeout",
        type=nonnegative_int_arg,
        default=0,
        help="Wall-clock timeout per subprocess in seconds. Defaults to --timeout + 900.",
    )
    args = parser.parse_args()

    try:
        args.run_id = validate_path_identity(args.run_id, name="run_id")
    except ValueError as exc:
        parser.error(str(exc))

    dataset_path = Path(os.path.abspath(args.dataset))
    predictions_path = Path(os.path.abspath(args.predictions))
    work_dir = Path(os.path.abspath(args.work_dir))
    try:
        for input_path in (dataset_path, predictions_path):
            parent_fd = _open_directory_no_symlinks(input_path.parent)
            os.close(parent_fd)
        ensure_directory_no_symlinks(work_dir)
        ensure_directory_no_symlinks(work_dir / "command_logs")
    except OSError as exc:
        parser.error(f"unsafe input or work directory: {exc}")

    try:
        queue = load_eval_queue(
            dataset_path,
            predictions_path,
            args.run_id,
            work_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit > 0:
        queue = queue[: args.limit]
    print(f"pending_non_empty_instances={len(queue)}")

    env = os.environ.copy()
    env.setdefault("OPENCOLLAB_DOCKER_API_TIMEOUT", "900")
    env.setdefault("DOCKER_CLIENT_TIMEOUT", "900")
    try:
        for key in ("OPENCOLLAB_DOCKER_API_TIMEOUT", "DOCKER_CLIENT_TIMEOUT"):
            env[key] = str(positive_timeout_seconds(env[key], name=key))
    except ValueError as exc:
        parser.error(str(exc))
    env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    workers = args.workers
    outer_timeout = args.outer_timeout if args.outer_timeout > 0 else args.timeout + 900
    print_lock = threading.Lock()
    failures: list[tuple[str, int]] = []
    stop_event = threading.Event()
    active_processes = ActiveProcessRegistry()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    try:
        futures = {
            pool.submit(
                run_one,
                iid=iid,
                model_name=model_name,
                identity=identity,
                prediction=prediction,
                ordinal=index,
                total=len(queue),
                dataset_path=dataset_path,
                work_dir=work_dir,
                run_id=args.run_id,
                timeout=args.timeout,
                namespace=args.namespace,
                cache_level=args.cache_level,
                clean=args.clean,
                outer_timeout=outer_timeout,
                env=env,
                print_lock=print_lock,
                active_processes=active_processes,
                stop_event=stop_event,
            ): iid
            for index, (iid, model_name, identity, prediction) in enumerate(queue, 1)
        }
        for future in as_completed(futures):
            expected_iid = futures[future]
            try:
                iid, returncode = future.result()
            except Exception as exc:
                iid, returncode = expected_iid, 70
                print(
                    f"failed {iid} with unhandled {type(exc).__name__}: {exc}",
                    flush=True,
                )
            if returncode != 0:
                failures.append((iid, returncode))
    except BaseException:
        stop_event.set()
        for future in futures:
            future.cancel()
        try:
            active_processes.terminate_all(sys.stderr)
        except BaseException:
            pass
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    if failures:
        print("failures=" + ", ".join(f"{iid}:{code}" for iid, code in failures), flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
