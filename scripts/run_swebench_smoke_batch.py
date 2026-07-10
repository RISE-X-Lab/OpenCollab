#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import math
import os
import signal
import select
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_records import (  # noqa: E402
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_RETAINED_BYTES,
    MAX_JSONL_RETAINED_ROWS,
    MAX_JSONL_SCAN_BYTES,
    RecordInputFormatError,
    RecordInputLimitError,
    is_completed_prediction,
    read_bounded_json,
)
from opencollab.adapters.safe_files import (  # noqa: E402
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    regular_path_identity,
)

_ORIGINAL_POPEN = subprocess.Popen
_GENERATOR_POPEN = subprocess.Popen

PROCESS_TERM_GRACE_SECONDS = 0.1
PROCESS_KILL_REAP_SECONDS = 2.0
PROCESS_CLEANUP_SLACK_SECONDS = 0.5
PROCESS_SPAWN_TIMEOUT_SECONDS = 10.0
TECHNICAL_EXIT_CODE = 125
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
SAFE_FILE_OPEN_RETRIES = 8
MAX_INSTANCE_DIRECTORY_ENTRIES = 10_000
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0


def positive_timeout_seconds(value: object, *, name: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return timeout


def _process_group_kwargs() -> dict:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        }
    return {}


def _posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_posix_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_leader(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except (subprocess.TimeoutExpired, ChildProcessError):
        return False


def _terminate_process_tree(
    process: subprocess.Popen,
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    if os.name == "posix":
        pgid = process.pid
        _signal_posix_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + term_timeout
        while _posix_group_exists(pgid) and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            _wait_leader(process, min(0.05, max(0.0, remaining)))
            if _posix_group_exists(pgid):
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if _posix_group_exists(pgid):
            _signal_posix_group(pgid, signal.SIGKILL)
        kill_deadline = time.monotonic() + kill_timeout
        leader_reaped = False
        while time.monotonic() < kill_deadline:
            remaining = kill_deadline - time.monotonic()
            leader_reaped = _wait_leader(process, min(0.05, remaining)) or leader_reaped
            if leader_reaped and not _posix_group_exists(pgid):
                return True
            time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
        return leader_reaped and not _posix_group_exists(pgid)

    if os.name == "nt":
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=kill_timeout,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            killed = False
        if not killed:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        return _wait_leader(process, kill_timeout)

    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        pass
    if _wait_leader(process, term_timeout):
        return True
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    return _wait_leader(process, kill_timeout)


def _ensure_process_tree_quiesced_after_wait(
    process: subprocess.Popen,
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    if os.name == "posix" and not _posix_group_exists(process.pid):
        return True
    if os.name not in {"posix", "nt"}:
        return _wait_leader(process, 0.0)
    return _terminate_process_tree(
        process,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def _generator_worker(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    stop: threading.Event,
    started: threading.Event,
    done: threading.Event,
    state: dict,
    term_timeout: float,
    kill_timeout: float,
) -> None:
    process: subprocess.Popen | None = None
    try:
        try:
            process = subprocess.Popen(cmd, cwd=cwd, env=env, **_process_group_kwargs())
        except BaseException as exc:
            state.update(status="spawn_error", error=exc)
            return
        finally:
            started.set()

        while True:
            if stop.is_set():
                state.update(
                    status="interrupted",
                    cleanup_ok=_terminate_process_tree(
                        process,
                        term_timeout=term_timeout,
                        kill_timeout=kill_timeout,
                    ),
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.update(
                    status="timeout",
                    cleanup_ok=_terminate_process_tree(
                        process,
                        term_timeout=term_timeout,
                        kill_timeout=kill_timeout,
                    ),
                )
                return
            try:
                returncode = process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
            state.update(
                status="completed",
                returncode=returncode,
                cleanup_ok=_ensure_process_tree_quiesced_after_wait(
                    process,
                    term_timeout=term_timeout,
                    kill_timeout=kill_timeout,
                ),
            )
            return
    except BaseException as exc:
        state.update(status="worker_error", error=exc)
        if process is not None:
            state["cleanup_ok"] = _terminate_process_tree(
                process,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
            )
    finally:
        done.set()


def _wait_event_resisting_interrupt(
    event: threading.Event,
    *,
    timeout: float,
    interrupt_event: threading.Event | None = None,
) -> tuple[bool, BaseException | None]:
    deadline = time.monotonic() + max(0.0, timeout)
    interruption: BaseException | None = None
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event.wait(min(0.05, remaining))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
                if interrupt_event is not None:
                    interrupt_event.set()
    return event.is_set(), interruption


def _install_termination_handlers(
    stop: threading.Event,
    previous: dict[int, object],
) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def request_stop(signum, _frame):
        stop.set()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)


def _restore_termination_handlers(previous: dict[int, object]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _run_generator_thread(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float = PROCESS_SPAWN_TIMEOUT_SECONDS,
    term_timeout: float = PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = PROCESS_KILL_REAP_SECONDS,
) -> tuple[int, str]:
    stop = threading.Event()
    started = threading.Event()
    done = threading.Event()
    state: dict = {}
    previous_handlers: dict[int, object] = {}
    worker = threading.Thread(
        target=_generator_worker,
        kwargs={
            "cmd": cmd,
            "cwd": cwd,
            "env": env,
            "deadline": time.monotonic() + outer_timeout,
            "stop": stop,
            "started": started,
            "done": done,
            "state": state,
            "term_timeout": term_timeout,
            "kill_timeout": kill_timeout,
        },
        name="swe-smoke-generator",
        daemon=False,
    )
    try:
        _install_termination_handlers(stop, previous_handlers)
        worker.start()

        spawn_finished, interruption = _wait_event_resisting_interrupt(
            started,
            timeout=spawn_timeout,
            interrupt_event=stop,
        )
        if not spawn_finished:
            stop.set()
            if interruption is not None:
                raise interruption
            return TECHNICAL_EXIT_CODE, "generator spawn exceeded its outer bound"
        if interruption is not None:
            raise interruption

        completed, interruption = _wait_event_resisting_interrupt(
            done,
            timeout=(
                outer_timeout
                + term_timeout
                + kill_timeout
                + PROCESS_CLEANUP_SLACK_SECONDS
            ),
            interrupt_event=stop,
        )
        if interruption is not None:
            raise interruption
        if not completed:
            stop.set()
            return TECHNICAL_EXIT_CODE, "generator cleanup exceeded its outer bound"

        status = state.get("status")
        if status == "completed":
            if not state.get("cleanup_ok"):
                return (
                    TECHNICAL_EXIT_CODE,
                    "generator leader exited but its process tree did not quiesce",
                )
            return int(state.get("returncode", TECHNICAL_EXIT_CODE)), ""
        if status == "timeout":
            if state.get("cleanup_ok"):
                return 124, f"generator exceeded outer timeout of {outer_timeout:g}s"
            return (
                TECHNICAL_EXIT_CODE,
                "generator timed out and its process tree did not quiesce",
            )
        if status == "spawn_error":
            return 127, f"generator failed to start: {state.get('error')}"
        return (
            TECHNICAL_EXIT_CODE,
            f"generator lifecycle failed: {state.get('error') or status}",
        )
    except (KeyboardInterrupt, SystemExit):
        stop.set()
        _wait_event_resisting_interrupt(
            done,
            timeout=term_timeout + kill_timeout + PROCESS_CLEANUP_SLACK_SECONDS,
        )
        raise
    finally:
        _restore_termination_handlers(previous_handlers)


_ORIGINAL_WAIT_EVENT = _wait_event_resisting_interrupt
_ORIGINAL_ENSURE_PROCESS_TREE_QUIESCED = _ensure_process_tree_quiesced_after_wait


def _helper_send(fd: int, value: dict) -> None:
    payload = (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("generator helper status write made no progress")
        view = view[written:]


def _generator_helper_main(
    write_fd: int,
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    deadline: float,
) -> None:
    try:
        os.setsid()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_DFL)
        _helper_send(write_fd, {"status": "helper_ready", "pgid": os.getpid()})
        try:
            process = _GENERATOR_POPEN(cmd, cwd=cwd, env=env)
        except BaseException as exc:
            _helper_send(
                write_fd,
                {
                    "status": "spawn_error",
                    "error": f"{type(exc).__name__}: {exc}"[:8_192],
                },
            )
        else:
            _helper_send(
                write_fd,
                {"status": "spawned", "pid": int(process.pid)},
            )
            remaining = max(0.0, deadline - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _helper_send(write_fd, {"status": "timeout"})
            except BaseException as exc:
                _helper_send(
                    write_fd,
                    {
                        "status": "worker_error",
                        "error": f"{type(exc).__name__}: {exc}"[:8_192],
                    },
                )
            else:
                _helper_send(
                    write_fd,
                    {"status": "completed", "returncode": int(returncode)},
                )
        # Stay alive as the owned process-group leader until the parent has
        # terminated and proven the whole group quiescent.
        while True:
            signal.pause()
    except BaseException:
        os._exit(TECHNICAL_EXIT_CODE)


def _read_helper_message(
    fd: int,
    buffer: bytearray,
    *,
    deadline: float,
) -> dict | None:
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("generator helper returned malformed status") from exc
            if not isinstance(value, dict):
                raise RuntimeError("generator helper returned non-object status")
            return value
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
        chunk = os.read(fd, 4096)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > 64 * 1024:
            raise RuntimeError("generator helper status exceeded its byte bound")


def _wait_helper_reaped(pid: int, *, deadline: float) -> bool:
    while True:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited == pid:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _cleanup_generator_helper(
    pid: int,
    *,
    ready: bool,
    term_timeout: float,
    kill_timeout: float,
) -> tuple[bool, BaseException | None]:
    interruption: BaseException | None = None

    def send(sig: signal.Signals) -> None:
        nonlocal interruption
        while True:
            try:
                if ready:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
                return
            except (ProcessLookupError, PermissionError):
                return
            except (KeyboardInterrupt, SystemExit) as exc:
                if interruption is None:
                    interruption = exc

    send(signal.SIGTERM)
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    while time.monotonic() < term_deadline:
        try:
            reaped = _wait_helper_reaped(
                pid,
                deadline=min(term_deadline, time.monotonic() + 0.02),
            )
            group_gone = not ready or not _posix_group_exists(pid)
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
            continue
        if reaped and group_gone:
            return True, interruption
    send(signal.SIGKILL)
    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    reaped = False
    while time.monotonic() < kill_deadline:
        try:
            reaped = _wait_helper_reaped(pid, deadline=kill_deadline) or reaped
            group_gone = not ready or not _posix_group_exists(pid)
            if reaped and group_gone:
                return True, interruption
            time.sleep(min(0.01, max(0.0, kill_deadline - time.monotonic())))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
    group_gone = not ready or not _posix_group_exists(pid)
    return reaped and group_gone, interruption


def _run_generator_helper(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float,
    term_timeout: float,
    kill_timeout: float,
) -> tuple[int, str]:
    started_at = time.monotonic()
    deadline = started_at + outer_timeout
    previous_handlers: dict[int, object] = {}
    signal_stop = threading.Event()
    _install_termination_handlers(signal_stop, previous_handlers)
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
        _restore_termination_handlers(previous_handlers)
        raise
    if pid == 0:
        os.close(read_fd)
        _generator_helper_main(
            write_fd,
            cmd=cmd,
            cwd=cwd,
            env=env,
            deadline=deadline,
        )
        os._exit(TECHNICAL_EXIT_CODE)
    os.close(write_fd)
    ready = False
    buffer = bytearray()
    outcome: dict | None = None
    cleanup_ok = False
    cleanup_interruption: BaseException | None = None
    try:
        spawn_deadline = min(deadline, started_at + spawn_timeout)
        while True:
            message = _read_helper_message(
                read_fd,
                buffer,
                deadline=spawn_deadline,
            )
            if message is None:
                outcome = {"status": "spawn_timeout"}
                break
            status = message.get("status")
            if status == "helper_ready":
                ready = True
                continue
            outcome = message
            break
        if outcome is not None and outcome.get("status") == "spawned":
            while True:
                message = _read_helper_message(read_fd, buffer, deadline=deadline)
                if message is None:
                    outcome = {"status": "timeout"}
                    break
                if message.get("status") in {
                    "completed",
                    "timeout",
                    "worker_error",
                }:
                    outcome = message
                    break
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        outcome = {
            "status": "worker_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        os.close(read_fd)
        cleanup_ok, cleanup_interruption = _cleanup_generator_helper(
            pid,
            ready=ready,
            term_timeout=term_timeout,
            kill_timeout=kill_timeout,
        )
        _restore_termination_handlers(previous_handlers)

    if cleanup_interruption is not None:
        raise cleanup_interruption

    status = (outcome or {}).get("status")
    if not cleanup_ok:
        return TECHNICAL_EXIT_CODE, "generator helper process tree did not quiesce"
    if status == "completed":
        return int(outcome.get("returncode", TECHNICAL_EXIT_CODE)), ""
    if status == "timeout":
        return 124, f"generator exceeded outer timeout of {outer_timeout:g}s"
    if status == "spawn_error":
        return 127, f"generator failed to start: {outcome.get('error')}"
    if status == "spawn_timeout":
        return TECHNICAL_EXIT_CODE, "generator spawn exceeded its outer bound"
    return (
        TECHNICAL_EXIT_CODE,
        f"generator lifecycle failed: {(outcome or {}).get('error') or status}",
    )


def _run_generator(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float = PROCESS_SPAWN_TIMEOUT_SECONDS,
    term_timeout: float = PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = PROCESS_KILL_REAP_SECONDS,
) -> tuple[int, str]:
    helper_compatible = (
        subprocess.Popen is _ORIGINAL_POPEN
        and _wait_event_resisting_interrupt is _ORIGINAL_WAIT_EVENT
        and _ensure_process_tree_quiesced_after_wait
        is _ORIGINAL_ENSURE_PROCESS_TREE_QUIESCED
    )
    if os.name == "posix" and helper_compatible:
        return _run_generator_helper(
            cmd,
            cwd=cwd,
            env=env,
            outer_timeout=outer_timeout,
            spawn_timeout=spawn_timeout,
            term_timeout=term_timeout,
            kill_timeout=kill_timeout,
        )
    return _run_generator_thread(
        cmd,
        cwd=cwd,
        env=env,
        outer_timeout=outer_timeout,
        spawn_timeout=spawn_timeout,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def _read_instance(path: Path) -> dict:
    document = read_bounded_json(path, max_bytes=MAX_INSTANCE_BYTES)
    if document is None or not isinstance(document[0], dict):
        raise ValueError(f"instance input is not a bounded regular JSON object: {path}")
    return document[0]


def _read_prediction_rows(path: Path) -> list[dict]:
    try:
        payload = read_regular_bytes(path, max_bytes=MAX_JSONL_SCAN_BYTES)
    except FileNotFoundError:
        return []
    retained_bytes = 0
    rows: list[dict] = []
    for raw_line in payload.splitlines(keepends=True):
        if not raw_line.strip():
            continue
        if len(raw_line) > MAX_JSONL_LINE_BYTES:
            raise RecordInputLimitError(
                f"JSONL line exceeds {MAX_JSONL_LINE_BYTES} bytes: {path}"
            )
        retained_bytes += len(raw_line)
        if (
            len(rows) >= MAX_JSONL_RETAINED_ROWS
            or retained_bytes > MAX_JSONL_RETAINED_BYTES
        ):
            raise RecordInputLimitError(
                f"JSONL input exceeds retained row or byte limit: {path}"
            )
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordInputFormatError(f"invalid JSONL record in {path}") from exc
        if not isinstance(value, dict):
            raise RecordInputFormatError(f"JSONL record must be an object: {path}")
        rows.append(value)
    return rows


def _prediction_has_patch(output: Path, instance_id: str) -> bool:
    latest: dict | None = None
    for record in _read_prediction_rows(output):
        if record.get("instance_id") == instance_id:
            latest = record
    return is_completed_prediction(latest)


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


def _open_regular_append(path: Path) -> int:
    path = Path(os.path.abspath(os.fspath(path)))
    ensure_directory_no_symlinks(path.parent)
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _parent_attempt in range(SAFE_FILE_OPEN_RETRIES):
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
                raise OSError(f"refusing non-regular smoke manifest: {path}")
            try:
                if before is None:
                    fd = os.open(
                        path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o644,
                        dir_fd=parent_fd,
                    )
                else:
                    fd = os.open(path.name, flags, dir_fd=parent_fd)
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
                    raise OSError(f"refusing non-regular smoke manifest: {path}")
                opened_identity = (opened.st_dev, opened.st_ino)
                if (current.st_dev, current.st_ino) != opened_identity:
                    continue
                if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                    continue
                if not _directory_path_matches_fd(path.parent, parent_fd):
                    break
                result_fd = fd
                fd = -1
                return result_fd
            except FileNotFoundError:
                pass
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(parent_fd)
    raise OSError(f"smoke manifest did not stabilize while opening: {path}")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting smoke manifest")
        view = view[written:]


def _append_manifest_record(path: Path, record: dict) -> None:
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = _open_regular_append(path)
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"manifest lock {path}")
        locked = True
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_MANIFEST_BYTES:
            raise OSError(f"smoke manifest exceeds byte limit: {path}")
        if needs_separator:
            _write_all(fd, b"\n")
        _write_all(fd, payload)
        os.fsync(fd)
        opened = os.fstat(fd)
        current_dev, current_ino, current_size, _mtime_ns, _ctime_ns = (
            regular_path_identity(path)
        )
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            current_dev,
            current_ino,
            current_size,
        ):
            raise OSError(f"smoke manifest changed while appending: {path}")
        _fsync_directory(path.parent)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _discover_instance_paths(instances_dir: Path, *, limit: int) -> list[Path]:
    try:
        root_info = instances_dir.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"instances directory does not exist: {instances_dir}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"instances path must be a real directory: {instances_dir}")
    paths: list[Path] = []
    scanned_entries = 0
    with os.scandir(instances_dir) as entries:
        for entry in entries:
            scanned_entries += 1
            if scanned_entries > MAX_INSTANCE_DIRECTORY_ENTRIES:
                raise ValueError(
                    "instances directory exceeds "
                    f"{MAX_INSTANCE_DIRECTORY_ENTRIES} entries"
                )
            if entry.name.endswith(".json"):
                paths.append(Path(entry.path))
    return sorted(paths)[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small OpenCollab SWE-bench smoke batch")
    parser.add_argument("--instances-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--budget", type=int, default=1_000_000)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--outer-timeout",
        type=float,
        default=None,
        help="Per-instance wall bound; defaults to --timeout plus 120 seconds.",
    )
    parser.add_argument(
        "--spawn-timeout",
        type=float,
        default=PROCESS_SPAWN_TIMEOUT_SECONDS,
    )
    parser.add_argument("--model-name", default="opencollab-glm52-single-smoke5")
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.budget <= 0:
        parser.error("--budget must be positive")
    if args.max_steps <= 0:
        parser.error("--max-steps must be positive")
    try:
        args.timeout = positive_timeout_seconds(args.timeout, name="--timeout")
        args.spawn_timeout = positive_timeout_seconds(
            args.spawn_timeout,
            name="--spawn-timeout",
        )
        outer_timeout = positive_timeout_seconds(
            args.outer_timeout if args.outer_timeout is not None else args.timeout + 120.0,
            name="--outer-timeout",
        )
    except ValueError as exc:
        parser.error(str(exc))

    instances_dir = Path(os.path.abspath(args.instances_dir))
    output_dir = Path(os.path.abspath(args.output_dir))
    try:
        ensure_directory_no_symlinks(output_dir)
    except OSError as exc:
        parser.error(f"unsafe output directory: {exc}")

    output_path = output_dir / "predictions.jsonl"
    manifest_path = output_dir / "manifest.jsonl"
    try:
        instance_paths = _discover_instance_paths(instances_dir, limit=args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    if not instance_paths:
        raise SystemExit(f"No instance JSON files found in {instances_dir}")

    env = os.environ.copy()
    default_cache_root = output_dir / ".cache"
    default_cache_paths = {
        "TMPDIR": default_cache_root / "tmp",
        "HF_HOME": default_cache_root / "hf",
        "HF_DATASETS_CACHE": default_cache_root / "datasets",
    }
    for key, path in default_cache_paths.items():
        configured = Path(env.get(key) or path)
        configured = Path(os.path.abspath(os.fspath(configured)))
        try:
            ensure_directory_no_symlinks(configured)
        except OSError:
            # Environment-provided cache aliases commonly traverse system
            # symlinks (for example /var on macOS).  Redirect them into the
            # owned output tree so every writable cache parent remains bound
            # to a verified lexical directory chain.
            configured = Path(os.path.abspath(os.fspath(path)))
            try:
                ensure_directory_no_symlinks(configured)
            except OSError as exc:
                parser.error(f"unsafe {key} directory: {exc}")
        env[key] = str(configured)
    arch_config = {
        "x86_64": ("x86_64", "linux/amd64"),
        "amd64": ("x86_64", "linux/amd64"),
        "arm64": ("arm64", "linux/arm64"),
        "aarch64": ("arm64", "linux/arm64"),
    }.get(args.arch)
    if arch_config is None:
        parser.error("--arch must be one of: x86_64, amd64, arm64, aarch64")
    spec_arch, docker_platform = arch_config
    env["DOCKER_DEFAULT_PLATFORM"] = docker_platform
    env.setdefault("OPENCOLLAB_DOCKER_TIMEOUT", "900")
    env.setdefault("OPENCOLLAB_TEMPERATURE", "0.2")
    env.setdefault("OPENCOLLAB_THINKING", "false")
    env.setdefault("OPENCOLLAB_LLM_TIMEOUT", "240")
    failures: list[str] = []

    for path in instance_paths:
        instance = _read_instance(path)
        instance_id = instance["instance_id"]
        spec = make_test_spec(instance, namespace="swebench", arch=spec_arch)
        image = spec.instance_image_key
        print(f"\n=== {instance_id} ===", flush=True)
        print(f"image: {image}", flush=True)

        if _prediction_has_patch(output_path, instance_id):
            print("prediction with patch already exists, skipping", flush=True)
            continue

        record = {
            "instance_id": instance_id,
            "instance_file": str(path),
            "image": image,
            "model_name": args.model_name,
        }
        _append_manifest_record(manifest_path, record)

        cmd = [
            sys.executable,
            str(REPO_ROOT / "swebench" / "gen_prediction.py"),
            "--instance-file",
            str(path),
            "--output",
            str(output_path),
            "--metrics",
            str(output_dir / "metrics.jsonl"),
            "--image",
            image,
            "--model-name",
            args.model_name,
            "--budget",
            str(args.budget),
            "--max-steps",
            str(args.max_steps),
            "--timeout",
            str(args.timeout),
        ]
        returncode, lifecycle_reason = _run_generator(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            outer_timeout=outer_timeout,
            spawn_timeout=args.spawn_timeout,
        )
        if returncode != 0:
            detail = f" ({lifecycle_reason})" if lifecycle_reason else ""
            print(
                f"instance failed with exit code {returncode}: {instance_id}{detail}",
                flush=True,
            )
            failures.append(instance_id)
        elif not _prediction_has_patch(output_path, instance_id):
            print(f"instance produced no non-empty patch: {instance_id}", flush=True)
            failures.append(instance_id)

    print(f"\nBatch output: {output_path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
