#!/usr/bin/env python3
"""One-command G1.1 runner for SWE-batch-pro-lite slices.

This script is deliberately narrow: it starts G1.1 generation for a contiguous
slice, runs the pro-lite direct evaluator for each non-empty patch, and writes a
machine and Markdown report. It avoids watch-loop restarts; each task has one
bounded generation attempt and one bounded evaluation attempt.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "jinan-aws"
DEFAULT_REMOTE_ROOT = "/nfsEDS/dongyh/data/kaka/docker/opencollab"
DEFAULT_BASE_RUN_DIR_PREFIX = (
    "/nfsEDS/dongyh/data/kaka/docker/opencollab/"
    "eval_work/validation_council_g11_16m_prolite26_35"
)
DEFAULT_MODEL_NAME = "opencollab-glm52-v1-16m-prolite26-35-20260707"
DEFAULT_REPORT_JSON = REPO_ROOT / "docs" / "monitoring" / "swe_g11_16m_prolite26_35_report.json"
DEFAULT_REPORT_MD = REPO_ROOT / "docs" / "monitoring" / "swe_g11_16m_prolite26_35_report.md"
DEFAULT_PROXY_ENV_FILE = Path.home() / ".claude" / "glm52.env"
DEFAULT_LOCAL_PROXY_BASE_URL = "http://127.0.0.1:8878"
REMOTE_HEALTH_SSH_TIMEOUT_FLOOR = 15
REMOTE_PROXY_TUNNELS: list[subprocess.Popen[str]] = []
LOCAL_PROCESS_TERM_GRACE_SECONDS = 5.0
LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS = 30.0
LOCAL_SPAWN_SIGNALS = frozenset((signal.SIGINT,))
PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS = 5.0
MAX_PROXY_ENV_BYTES = 1024 * 1024
MAX_TASKS_PER_RUN = 1000
MAX_REMOTE_OUTPUT_TAIL_CHARS = 4 * 1024 * 1024

SYNC_FILES = [
    "scripts/run_swe_v2_one_from_fifo.sh",
    "swebench/gen_prediction.py",
    "swebench/gen_prediction_workflow.py",
    "workflows/validation_council_solve.py",
]

SYNC_DIRS = [
    "opencollab/opencollab",
]


REMOTE_RUNNER = r'''
import atexit
import ast
from collections import deque
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid


cfg = json.loads(sys.stdin.read())
token = cfg["token"]
owner_nonce = cfg["owner_nonce"]
remote_root = pathlib.Path(cfg["remote_root"])
remote_repo = pathlib.Path(cfg["remote_repo"])
base_run_dir = pathlib.Path(cfg["base_run_dir"])
package_root = remote_repo / "opencollab"
if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_INELIGIBLE,
    metric_submission_integrity,
    open_regular_binary,
)

dataset_path = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
workflow = cfg["workflow"]
model_name = cfg["model_name"]
session_prefix = cfg["session_prefix"].rstrip("_")
remote_proxy_base_url = cfg["remote_proxy_base_url"].rstrip("/")
start_index = int(cfg["start_index"])
limit = int(cfg["limit"])
budget = int(cfg["budget"])
max_steps = int(cfg["max_steps"])
swe_timeout = int(cfg["swe_timeout"])
task_wall_timeout = int(cfg["task_wall_timeout"])
eval_timeout = int(cfg["eval_timeout"])
checkpoint_interval = int(cfg["checkpoint_interval"])
max_task_starts = int(cfg["max_task_starts"])
dry_run = bool(cfg["dry_run"])
ACTIVE_CHILD_PGIDS = set()
ACTIVE_FIFO_PATHS = set()
RUNNER_LOCK_FD = None
RUNNER_OWNER_RECORD = None
RUNNER_STATE_THREAD_LOCK = threading.RLock()
PROCESS_TERM_GRACE_SECONDS = 30.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
PROCESS_CLEANUP_FAILED_EXIT_CODE = 125
SPAWN_SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_JSONL_RETAINED_BYTES = 64 * 1024 * 1024
MAX_JSONL_RETAINED_ROWS = 10000
MAX_JSONL_SCAN_BYTES = 256 * 1024 * 1024
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_ROWS = 1_000_000
MAX_LOG_TAIL_BYTES = 4 * 1024 * 1024
MAX_TASK_ID_BYTES = 240
MAX_TASKS_PER_RUN = 1000
MAX_DURABLE_JSONL_BYTES = 256 * 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_EXIT_STATUS_BYTES = 128
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0


class RecordInputLimitError(ValueError):
    pass


class RecordInputFormatError(ValueError):
    pass


def block_spawn_signals():
    state = {"previous": {}, "pending": [], "restored": False}

    def defer(signum, frame):
        if signum not in state["pending"]:
            state["pending"].append(signum)

    try:
        for signum in SPAWN_SIGNALS:
            state["previous"][signum] = signal.getsignal(signum)
            signal.signal(signum, defer)
    except BaseException:
        for signum, handler in state["previous"].items():
            signal.signal(signum, handler)
        raise
    return state


def restore_spawn_signals(state):
    if state.get("restored"):
        return
    for signum, handler in state["previous"].items():
        signal.signal(signum, handler)
    state["restored"] = True
    for signum in state["pending"]:
        handler = state["previous"].get(signum, signal.SIG_DFL)
        if handler == signal.SIG_IGN:
            continue
        if handler == signal.SIG_DFL:
            os.kill(os.getpid(), signum)
        else:
            handler(signum, None)


def wait_for_owned_cleanup(done, timeout):
    deadline = time.monotonic() + max(0.0, timeout)
    interruption = None
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


def consume_process_exit(proc):
    try:
        proc.wait()
    except BaseException:
        pass
    while process_group_exists(proc.pid):
        time.sleep(0.1)
    ACTIVE_CHILD_PGIDS.discard(proc.pid)


def schedule_process_exit_consumer(proc):
    threading.Thread(
        target=consume_process_exit,
        args=(proc,),
        name=f"prolite-reap-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def process_group_exists(pgid):
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(pgid, deadline):
    while process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def terminate_process_group_owned(proc, term_timeout, kill_timeout):
    pgid = proc.pid
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            schedule_process_exit_consumer(proc)
            return False

    leader_reaped = False
    try:
        proc.wait(timeout=max(0.0, term_deadline - time.monotonic()))
        leader_reaped = True
    except ChildProcessError:
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass

    group_gone = wait_for_process_group_exit(pgid, term_deadline)
    if leader_reaped and group_gone:
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    if not leader_reaped:
        try:
            proc.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
            leader_reaped = True
        except ChildProcessError:
            leader_reaped = True
        except subprocess.TimeoutExpired:
            pass
    group_gone = wait_for_process_group_exit(pgid, kill_deadline)
    if not leader_reaped or not group_gone:
        schedule_process_exit_consumer(proc)
    return leader_reaped and group_gone


def terminate_process_group_bounded(
    proc,
    term_timeout=PROCESS_TERM_GRACE_SECONDS,
    kill_timeout=PROCESS_KILL_REAP_TIMEOUT_SECONDS,
):
    state = {}
    done = threading.Event()

    def cleanup():
        try:
            state["reaped"] = terminate_process_group_owned(
                proc,
                term_timeout,
                kill_timeout,
            )
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    cleanup_thread = threading.Thread(
        target=cleanup,
        name=f"prolite-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    cleanup_thread.start()
    completed, interruption = wait_for_owned_cleanup(
        done,
        term_timeout + kill_timeout + PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
    )
    if completed and "reaped" in state:
        reaped = bool(state["reaped"])
    else:
        reaped = False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
    if interruption is not None:
        raise interruption
    return reaped


def ensure_process_group_quiesced_after_wait(
    proc,
    term_timeout=PROCESS_TERM_GRACE_SECONDS,
    kill_timeout=PROCESS_KILL_REAP_TIMEOUT_SECONDS,
):
    if not process_group_exists(proc.pid):
        return True
    return terminate_process_group_bounded(
        proc,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def slice_label():
    end_index = start_index + max(limit, 0) - 1
    return str(start_index) if end_index <= start_index else f"{start_index}-{end_index}"


def terminate_active_children(_sig=signal.SIGTERM):
    owned = set(ACTIVE_CHILD_PGIDS)
    for pgid in owned:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            ACTIVE_CHILD_PGIDS.discard(pgid)
    term_deadline = time.monotonic() + PROCESS_TERM_GRACE_SECONDS
    for pgid in list(owned):
        if wait_for_process_group_exit(pgid, term_deadline):
            ACTIVE_CHILD_PGIDS.discard(pgid)
            owned.discard(pgid)
    for pgid in owned:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            ACTIVE_CHILD_PGIDS.discard(pgid)
    kill_deadline = time.monotonic() + PROCESS_KILL_REAP_TIMEOUT_SECONDS
    for pgid in list(owned):
        if wait_for_process_group_exit(pgid, kill_deadline):
            ACTIVE_CHILD_PGIDS.discard(pgid)
            owned.discard(pgid)
    return not owned


def cleanup_fifo(path):
    try:
        pathlib.Path(path).unlink(missing_ok=True)
    finally:
        ACTIVE_FIFO_PATHS.discard(pathlib.Path(path))


def cleanup_active_fifos():
    for path in list(ACTIVE_FIFO_PATHS):
        cleanup_fifo(path)


def signal_exit(signum, frame):
    terminate_active_children(signal.SIGTERM)
    cleanup_active_fifos()
    raise SystemExit(128 + int(signum))


def fsync_directory(path):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short harness file write")
        view = view[written:]


def open_regular_file(path, flags, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_flags = (
        flags
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"harness file must be regular: {path}")
        try:
            if before is None:
                fd = os.open(path, safe_flags | os.O_CREAT | os.O_EXCL, mode)
            else:
                fd = os.open(path, safe_flags)
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
                or (
                    before is not None
                    and (before.st_dev, before.st_ino) != identity
                )
            ):
                os.close(fd)
                continue
            return fd
        except BaseException:
            os.close(fd)
            raise
    raise OSError(f"harness file did not stabilize while opening: {path}")


def acquire_lock(fd, label):
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
            raise TimeoutError(f"timed out acquiring {label}")
        time.sleep(min(0.01, remaining))


@contextmanager
def open_locked_append(path):
    fd = open_regular_file(path, os.O_RDWR | os.O_APPEND)
    locked = False
    handle = None
    try:
        acquire_lock(fd, f"append lock {path}")
        locked = True
        handle = os.fdopen(fd, "ab", closefd=False)
        yield handle
        handle.flush()
        os.fsync(fd)
        fsync_directory(path.parent)
    finally:
        if handle is not None:
            handle.close()
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"harness destination must be regular or absent: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if before is None:
            if current is not None:
                raise OSError(f"harness destination appeared during write: {path}")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError(f"harness destination changed during write: {path}")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def process_start_identity(pid):
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        if len(remainder) > 19 and remainder[19].isdigit():
            return f"proc:{remainder[19]}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""


def _runner_owner_record():
    try:
        context = open_regular_binary(base_run_dir / "runner.pid")
        handle = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_size <= 0 or opened.st_size > 4096:
            raise RuntimeError("runner owner must be a bounded regular file")
        raw = handle.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("runner owner exceeds its byte limit")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runner owner record is invalid") from exc
    finally:
        context.__exit__(None, None, None)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "opencollab.prolite_runner_owner.v1"
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pid"), int)
        or value["pid"] <= 1
        or not isinstance(value.get("start_identity"), str)
        or not value["start_identity"]
        or re.fullmatch(r"[0-9a-f]{32}", str(value.get("owner_nonce") or "")) is None
    ):
        raise RuntimeError("runner owner record is invalid")
    return value


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def write_runner_pid():
    global RUNNER_LOCK_FD, RUNNER_OWNER_RECORD
    if RUNNER_LOCK_FD is not None:
        return RUNNER_OWNER_RECORD
    base_run_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    start_identity = process_start_identity(pid)
    if not start_identity or not re.fullmatch(r"[0-9a-f]{32}", owner_nonce):
        raise RuntimeError("runner ownership identity could not be established")
    lock_fd = open_regular_file(base_run_dir / ".runner.lock", os.O_RDWR)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("another ProLite runner owns this run directory") from exc
            raise
        existing = _runner_owner_record()
        if existing is not None:
            existing_pid = existing["pid"]
            current_identity = process_start_identity(existing_pid)
            if existing_pid == pid and existing.get("owner_nonce") == owner_nonce:
                if existing.get("start_identity") != start_identity:
                    raise RuntimeError("current runner owner identity changed")
            elif _pid_exists(existing_pid):
                if not current_identity:
                    raise RuntimeError("existing runner owner identity is unverifiable")
                if current_identity == existing["start_identity"]:
                    raise RuntimeError("a live ProLite runner already owns this run directory")
        record = {
            "schema": "opencollab.prolite_runner_owner.v1",
            "pid": pid,
            "start_identity": start_identity,
            "owner_nonce": owner_nonce,
        }
        atomic_write_bytes(
            base_run_dir / "runner.pid",
            (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"),
        )
        RUNNER_LOCK_FD = lock_fd
        RUNNER_OWNER_RECORD = record
        lock_fd = -1
        return record
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def initialize_runner_ownership():
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal_exit)
    write_runner_pid()
    atexit.register(cleanup_active_fifos)
    atexit.register(terminate_active_children)


if __name__ == "__main__":
    initialize_runner_ownership()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def iter_jsonl(path, max_scan_bytes=None, max_rows=None):
    try:
        context = open_regular_binary(path)
        handle = context.__enter__()
    except FileNotFoundError:
        return
    try:
        opened = os.fstat(handle.fileno())
        if max_scan_bytes is not None and opened.st_size > max_scan_bytes:
            raise RecordInputLimitError(
                f"JSONL input exceeds {max_scan_bytes} bytes: {path}"
            )
        remaining = opened.st_size
        physical_rows = 0
        while True:
            if remaining <= 0:
                break
            line = handle.readline(min(MAX_JSONL_LINE_BYTES + 1, remaining))
            if not line:
                break
            remaining -= len(line)
            physical_rows += 1
            if max_rows is not None and physical_rows > max_rows:
                raise RecordInputLimitError(
                    f"JSONL input exceeds {max_rows} physical rows: {path}"
                )
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise RecordInputLimitError(
                    f"JSONL line exceeds {MAX_JSONL_LINE_BYTES} bytes: {path}"
                )
            if not line.strip():
                raise RecordInputFormatError(f"blank JSONL record in {path}")
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecordInputFormatError(
                    f"invalid JSONL record in {path}"
                ) from exc
            if not isinstance(value, dict):
                raise RecordInputFormatError(
                    f"JSONL record must be an object: {path}"
                )
            yield len(line), value
    finally:
        context.__exit__(None, None, None)


def read_jsonl(path):
    rows = deque()
    retained_bytes = 0
    for line_size, value in iter_jsonl(
        path,
        max_scan_bytes=MAX_JSONL_SCAN_BYTES,
    ):
        rows.append((line_size, value))
        retained_bytes += line_size
        if (
            len(rows) > MAX_JSONL_RETAINED_ROWS
            or retained_bytes > MAX_JSONL_RETAINED_BYTES
        ):
            raise RecordInputLimitError(
                "JSONL input exceeds retained row or byte limit: "
                f"{path}"
            )
    return [value for _size, value in rows]


def read_tail_text(path, limit=4000):
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        return ""
    limit = min(limit, MAX_LOG_TAIL_BYTES)
    if limit == 0:
        return ""
    try:
        with open_regular_binary(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - limit), os.SEEK_SET)
            return handle.read(limit).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def write_json(path, value):
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def append_jsonl(path, value):
    payload = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_JSONL_LINE_BYTES:
        raise RecordInputLimitError(f"JSONL row exceeds byte limit: {path}")
    fd = open_regular_file(path, os.O_RDWR | os.O_APPEND)
    locked = False
    try:
        acquire_lock(fd, f"JSONL output lock {path}")
        locked = True
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_DURABLE_JSONL_BYTES:
            raise RecordInputLimitError(f"JSONL output exceeds byte limit: {path}")
        if needs_separator:
            write_all(fd, b"\n")
        write_all(fd, payload)
        os.fsync(fd)
        fsync_directory(path.parent)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def run(args, timeout=60):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"returncode": 124, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def http_health(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 400, "status": response.status, "body": body}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def load_dataset(selected_start, selected_limit):
    if not dataset_path.exists():
        raise RuntimeError(f"missing dataset: {dataset_path}")
    rows = []
    for index, (_line_size, value) in enumerate(
        iter_jsonl(
            dataset_path,
            max_scan_bytes=MAX_DATASET_BYTES,
            max_rows=MAX_DATASET_ROWS,
        ),
        1,
    ):
        if index < selected_start:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"dataset row {index} must be an object")
        row = dict(value)
        row["instance_id"] = validate_task_identity(row.get("instance_id"))
        rows.append(row)
        if len(rows) >= selected_limit:
            break
    return rows


def validate_task_identity(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows_path = pathlib.PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows_path.is_absolute()
        or windows_path.drive
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("instance_id must be one non-empty path component")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise ValueError(
            "instance_id must not contain control, format, or surrogate characters"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_TASK_ID_BYTES:
        raise ValueError(
            f"instance_id exceeds {MAX_TASK_ID_BYTES} UTF-8 bytes"
        )
    return value


def parse_literal_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item)]
    return [text]


def prediction_patch(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("model_patch") or row.get("patch") or "")


def is_eval_test_path(path):
    normalized = str(path or "").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if any(part in {"test", "tests", "__tests__"} for part in parts):
        return True
    return (
        name.endswith("_test.go")
        or name.startswith("test_") and name.endswith(".py")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def decode_git_c_path(value):
    value = str(value or "")
    quoted = value.startswith('"')
    index = 1 if quoted else 0
    decoded = bytearray()
    while index < len(value):
        char = value[index]
        if quoted and char == '"':
            break
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogatepass"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            decoded.append(ord("\\"))
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(GIT_C_ESCAPES.get(escaped, ord(escaped)))
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def git_header_tokens(header):
    text = str(header or "").strip()
    prefix = "diff --git "
    if not text.startswith(prefix):
        return []
    text = text[len(prefix):]
    tokens = []
    index = 0
    while index < len(text) and len(tokens) < 2:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        start = index
        if text[index] == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace():
                index += 1
        tokens.append(text[start:index])
    return tokens


def diff_target_path(header):
    match = re.match(r"^diff --git a/(.*) b/(.*)$", str(header or "").strip())
    if match:
        return match.group(2)
    paths = git_header_tokens(header)
    if len(paths) >= 2:
        target = decode_git_c_path(paths[1])
        if target.startswith("b/"):
            return target[2:]
    if paths:
        source = decode_git_c_path(paths[0])
        if source.startswith("a/"):
            return source[2:]
    return ""


def filter_model_patch_for_eval(patch):
    if not patch.strip():
        return patch
    blocks = []
    current = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    kept = []
    for block in blocks:
        header = block[0] if block else ""
        path = diff_target_path(header)
        if path and is_eval_test_path(path):
            continue
        kept.extend(block)
    return "".join(kept)


def eval_model_patch(prediction):
    return filter_model_patch_for_eval(prediction_patch(prediction))


def row_task_id(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("instance_id") or row.get("task_id") or "")


def row_record_id(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("record_id") or row.get("attempt_id") or "")


def patch_sha(patch):
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def row_patch_sha(row):
    if not isinstance(row, dict):
        return ""
    patch = prediction_patch(row)
    if patch:
        return patch_sha(patch)
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def row_explicit_patch_sha(row):
    if not isinstance(row, dict):
        return ""
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def embedded_workflow_metric(row):
    if not isinstance(row, dict):
        return None
    metric = row.get("workflow_metric")
    if not isinstance(metric, dict):
        return None
    if row_task_id(metric) != row_task_id(row):
        return None
    if row_record_id(metric) != row_record_id(row):
        return None
    prediction_sha = row_patch_sha(row)
    metric_sha = row_patch_sha(metric)
    if not prediction_sha or not metric_sha or not patch_sha_matches(prediction_sha, metric_sha):
        return None
    return metric


def patch_sha_matches(left, right):
    left = str(left or "")
    right = str(right or "")
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{64}", left)
        and re.fullmatch(r"[0-9a-fA-F]{64}", right)
        and left == right
    )


def workflow_status(row):
    if not isinstance(row, dict):
        return ""
    result = row.get("workflow_result") if isinstance(row.get("workflow_result"), dict) else {}
    return str(row.get("workflow_status") or result.get("status") or "")


def latest_pair(run_dir, task):
    predictions = [row for row in read_jsonl(run_dir / "predictions.jsonl") if row_task_id(row) == task]
    metrics = [row for row in read_jsonl(run_dir / "metrics.jsonl") if row_task_id(row) == task]
    if not predictions:
        return None, None, "missing_prediction"
    prediction = predictions[-1]
    record_id = row_record_id(prediction)
    current_sha = row_patch_sha(prediction)
    if record_id:
        matched = [row for row in metrics if row_record_id(row) == record_id]
        if not matched:
            embedded_metric = embedded_workflow_metric(prediction)
            if embedded_metric is not None:
                return prediction, embedded_metric, "embedded_metric"
            return prediction, None, "missing_metric_for_record_id"
        metric = matched[-1]
        metric_sha = row_patch_sha(metric)
        if current_sha and metric_sha and not patch_sha_matches(metric_sha, current_sha):
            return prediction, None, "record_id_patch_sha_mismatch"
        if current_sha and not metric_sha:
            return prediction, None, "record_id_patch_sha_missing"
        return prediction, metric, "record_id"
    if current_sha:
        for metric in reversed(metrics):
            metric_sha = row_patch_sha(metric)
            if metric_sha and patch_sha_matches(metric_sha, current_sha):
                return prediction, metric, "patch_sha"
    return prediction, metrics[-1] if metrics else None, "legacy_latest"


def generation_done(run_dir, task):
    prediction, metric, pairing = latest_pair(run_dir, task)
    return completed_generation_identity(prediction, metric, task), prediction, metric, pairing


def completed_generation_identity(prediction, metric, task):
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        return False
    original_patch = prediction_patch(prediction)
    if not original_patch.strip() or not eval_model_patch(prediction).strip():
        return False
    if row_task_id(prediction) != task or row_task_id(metric) != task:
        return False
    prediction_record_id = row_record_id(prediction)
    if not prediction_record_id or row_record_id(metric) != prediction_record_id:
        return False
    computed_sha = patch_sha(original_patch)
    if not patch_sha_matches(row_explicit_patch_sha(prediction), computed_sha):
        return False
    if not patch_sha_matches(row_explicit_patch_sha(metric), computed_sha):
        return False
    if metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE:
        return False
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return False
    status = workflow_status(metric)
    if status == "done":
        return returncode == 0
    if status == "done_with_timeout_patch":
        return returncode == 124
    return False


def generation_done_result(task, prediction, metric, pairing, **extra):
    result = {
        "status": "generation_done",
        "task": task,
        "pairing": pairing,
        "patch_len": len(eval_model_patch(prediction)),
        "original_patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
    }
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def image_for_row(row):
    tag = str(row.get("dockerhub_tag") or row.get("image_tag") or "")
    if tag:
        if tag.startswith("docker."):
            return tag
        return "docker.1panel.live/jefzda/sweap-images:" + tag
    task = str(row.get("instance_id") or "")
    key = task[len("instance_"):] if task.startswith("instance_") else task
    return "docker.1panel.live/jefzda/sweap-images:" + key


def image_exists(image):
    return run(["docker", "image", "inspect", image], timeout=120)["returncode"] == 0


def ensure_image(image):
    if image_exists(image):
        return {"ok": True, "image": image}
    prefix = "docker.1panel.live/"
    alias = image[len(prefix):] if image.startswith(prefix) else ""
    if alias and image_exists(alias):
        tagged = run(["docker", "tag", alias, image], timeout=120)
        if tagged["returncode"] == 0:
            return {"ok": True, "image": image, "aliased_from": alias}
        return {
            "ok": False,
            "image": image,
            "alias": alias,
            "reason": "tag_failed",
            "details": tagged["stderr"] or tagged["stdout"],
        }
    pulled = run(["docker", "pull", image], timeout=900)
    if pulled["returncode"] == 0 and image_exists(image):
        return {"ok": True, "image": image, "pulled": True}
    if alias:
        pulled_alias = run(["docker", "pull", alias], timeout=900)
        if pulled_alias["returncode"] == 0 and image_exists(alias):
            tagged = run(["docker", "tag", alias, image], timeout=120)
            if tagged["returncode"] == 0:
                return {"ok": True, "image": image, "aliased_from": alias, "pulled_alias": True}
            return {
                "ok": False,
                "image": image,
                "alias": alias,
                "reason": "tag_failed",
                "details": tagged["stderr"] or tagged["stdout"],
            }
    return {
        "ok": False,
        "image": image,
        "alias": alias,
        "reason": "missing_image",
        "details": pulled["stderr"] or pulled["stdout"],
    }


def image_repo_workdir_status(image):
    script = r"""
if [ -d /testbed/.git ] || [ -d /app/.git ] || [ -d /workspace/.git ] || [ -d /repo/.git ] || [ -d /src/.git ]; then
  exit 0
fi
found=$(find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 || true)
if [ -n "$found" ]; then
  exit 0
fi
echo "no repository checkout found under common paths" >&2
exit 2
"""
    result = run(
        ["docker", "run", "--rm", "--entrypoint", "", image, "bash", "-lc", script],
        timeout=120,
    )
    return {
        "ok": result["returncode"] == 0,
        "image": image,
        "returncode": result["returncode"],
        "details": result["stderr"] or result["stdout"],
    }


def task_session(task):
    issue = task.split("__", 1)[1] if "__" in task else task
    issue = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue.replace("-", "_").replace("/", "_"))
    return f"{session_prefix}_{issue}"


def generation_state_path(run_dir):
    return run_dir / "generation.state.json"


def load_json(path):
    try:
        context = open_regular_binary(path)
        handle = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_size > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        raw = handle.read(MAX_JSON_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        context.__exit__(None, None, None)


def start_count(run_dir):
    state = load_json(generation_state_path(run_dir))
    if not isinstance(state, dict):
        return 0
    try:
        return int(state.get("start_count") or 0)
    except Exception:
        return 0


def write_start_state(run_dir, task, session):
    if RUNNER_LOCK_FD is None:
        raise RuntimeError("runner directory ownership lock is not held")
    with RUNNER_STATE_THREAD_LOCK:
        state = load_json(generation_state_path(run_dir))
        if not isinstance(state, dict):
            state = {}
        starts = state.get("starts") if isinstance(state.get("starts"), list) else []
        try:
            previous_count = int(state.get("start_count") or 0)
        except (TypeError, ValueError):
            previous_count = 0
        count = previous_count + 1
        event = {"started_at": now(), "session": session, "workflow": workflow}
        starts.append(event)
        state.update({
            "schema": "opencollab.generation_state.v1",
            "task": task,
            "start_count": count,
            "last_started_at": event["started_at"],
            "last_session": session,
            "starts": starts[-20:],
        })
        write_json(generation_state_path(run_dir), state)
        return state


def write_fifo_with_timeout(path, text, timeout=45):
    data = text.encode("utf-8")
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue
        try:
            offset = 0
            while offset < len(data):
                if time.time() >= deadline:
                    return {
                        "ok": False,
                        "error": "timed out while writing complete fifo payload",
                    }
                try:
                    written = os.write(fd, data[offset:])
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if written <= 0:
                    return {"ok": False, "error": "zero-byte fifo write"}
                offset += written
            return {"ok": True}
        except OSError as exc:
            last_error = str(exc)
        finally:
            os.close(fd)
    return {"ok": False, "error": last_error or "timed out waiting for fifo reader"}


def compact_python_test_targets(tests, selected, max_args=80, max_chars=24000):
    targets = [str(item) for item in (tests or selected) if str(item)]
    if not targets:
        return []
    quoted = " ".join(shlex.quote(item) for item in targets)
    if len(targets) <= max_args and len(quoted) <= max_chars:
        return targets
    files = []
    for item in targets:
        path = item.split("::", 1)[0]
        if path and path not in files:
            files.append(path)
    return files or targets[:max_args]


def go_test_packages(tests, selected):
    packages = []
    for raw in tests or selected:
        item = str(raw or "").split(" | ", 1)[0].split("::", 1)[0].strip()
        if not item:
            continue
        if item.endswith(".go"):
            package = str(pathlib.Path(item).parent).replace("\\", "/")
        elif "/" in item:
            package = item.strip("/")
            if package and not package.endswith("..."):
                package = package.rstrip("/") + "/..."
        else:
            continue
        if package in {"", "."}:
            target = "./..."
        elif package.startswith("./"):
            target = package
        else:
            target = "./" + package
        if target not in packages:
            packages.append(target)
    return packages


def js_runner_command(binary, package_script, target, extra_args=""):
    local_binary = f"./node_modules/.bin/{binary}"
    target_part = f" {target}" if target else ""
    extra_part = f" {extra_args}" if extra_args else ""
    package_script = shlex.quote(package_script)
    return "\n".join([
        "if [ -x " + shlex.quote(local_binary) + " ]; then",
        "  " + shlex.quote(local_binary) + extra_part + target_part,
        "elif command -v yarn >/dev/null 2>&1; then",
        f"  yarn {package_script}{extra_part}{target_part}",
        "elif command -v npx >/dev/null 2>&1; then",
        f"  npx {shlex.quote(binary)}{extra_part}{target_part}",
        "elif command -v pnpm >/dev/null 2>&1; then",
        f"  pnpm {package_script} --{extra_part}{target_part}",
        "elif command -v corepack >/dev/null 2>&1; then",
        f"  corepack pnpm {package_script} --{extra_part}{target_part}",
        "else",
        f"  echo 'No supported JS test runner found for {binary}' >&2",
        "  exit 127",
        "fi",
    ])


_NOOP_TEST_COMMANDS = {"", "true", ":", "/bin/true"}


def _is_runnable_test_command(cmd):
    # A no-op like `true` "passes" instantly and would score a false green:
    # resolved=true having executed zero target tests. Treat it as no command.
    return bool(cmd) and cmd.strip() not in _NOOP_TEST_COMMANDS


def prolite_test_command(row, tests):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if language == "python" or any("::" in item for item in tests):
        targets = compact_python_test_targets(tests, selected)
        if targets:
            return "python3 -m pytest -q " + " ".join(shlex.quote(item) for item in targets)
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        packages = go_test_packages(tests, selected)
        if packages:
            return "go test " + " ".join(shlex.quote(package) for package in packages)
        return "go test ./..."
    if language in {"js", "javascript", "typescript"} or repo in {
        "nodebb/nodebb",
        "protonmail/webclients",
        "element-hq/element-web",
    }:
        files = [
            item.split(" | ", 1)[0]
            for item in (selected or tests)
            if item and ("/" in item or "." in item)
        ]
        seen = []
        for item in files:
            if item not in seen:
                seen.append(item)
        target = " ".join(shlex.quote(item) for item in seen)
        if repo == "nodebb/nodebb":
            return js_runner_command("mocha", "test", target, "--timeout 30000")
        if repo == "element-hq/element-web":
            return js_runner_command("jest", "test", target)
        return js_runner_command("jest", "test", target)
    return str(row.get("test_cmd") or row.get("eval_cmd") or "")


def prolite_service_bootstrap(row):
    repo = str(row.get("repo") or "").lower()
    hints = " ".join(str(row.get(key) or "") for key in ("database", "before_repo_set_cmd", "test_cmd", "eval_cmd")).lower()
    needs_redis = repo == "nodebb/nodebb" or "redis" in hints
    if not needs_redis:
        return ""
    return r"""
redis_ready() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG && return 0
  fi
  (echo > /dev/tcp/127.0.0.1/6379) >/dev/null 2>&1 && return 0
  return 1
}

if redis_ready; then
  echo "redis already ready on 127.0.0.1:6379"
  exit 0
fi

if command -v redis-server >/dev/null 2>&1; then
  mkdir -p /tmp/opencollab-redis
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /tmp/opencollab-redis --save "" --appendonly no >/tmp/prolite_redis_server.log 2>&1 || true
elif command -v service >/dev/null 2>&1; then
  service redis-server start >/tmp/prolite_redis_server.log 2>&1 || service redis start >>/tmp/prolite_redis_server.log 2>&1 || true
else
  echo "redis-server not found and service command unavailable" >&2
  exit 42
fi

for _attempt in $(seq 1 100); do
  if redis_ready; then
    echo "redis ready on 127.0.0.1:6379"
    exit 0
  fi
  sleep 0.1
done

echo "redis did not become ready on 127.0.0.1:6379" >&2
cat /tmp/prolite_redis_server.log 2>/dev/null || true
exit 42
"""


def generation_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True, exist_ok=True)
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(task, prediction, metric, pairing)
    if start_count(run_dir) >= max_task_starts:
        return {"status": "generation_start_limit_reached", "task": task, "start_count": start_count(run_dir)}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_generation_image", "task": task, "image_status": image_status}
    if dry_run:
        workdir_status = image_repo_workdir_status(image)
        if not workdir_status.get("ok"):
            return {
                "status": "blocked_bad_generation_workdir",
                "task": task,
                "image_status": image_status,
                "workdir_status": workdir_status,
            }
        return {"status": "would_generate", "task": task, "image": image, "workdir_status": workdir_status}
    fifo = pathlib.Path("/tmp") / (
        f"opencollab_v1_{os.getpid()}_{uuid.uuid4().hex}.fifo"
    )
    os.mkfifo(fifo, 0o600)
    ACTIVE_FIFO_PATHS.add(fifo)
    session = task_session(task)
    state = write_start_state(run_dir, task, session)
    log_path = run_dir / "generation_logs" / f"{task}.outer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "OPENCOLLAB_SWE_GENERATOR": "workflow",
        "OPENCOLLAB_SWE_WORKFLOW": workflow,
        "OPENCOLLAB_SWE_MODEL_NAME": model_name,
        "OPENCOLLAB_SWE_BUDGET": str(budget),
        "OPENCOLLAB_SWE_MAX_STEPS": str(max_steps),
        "OPENCOLLAB_SWE_TIMEOUT": str(swe_timeout),
        "OPENCOLLAB_LLM_TIMEOUT": str(cfg["llm_timeout"]),
        "OPENCOLLAB_SWE_DATASET": "swe-batch-pro-lite",
        "OPENCOLLAB_REMOTE_PROXY_BASE_URL": remote_proxy_base_url,
        "OPENCOLLAB_SWE_CHECKPOINT_INTERVAL_SECONDS": str(checkpoint_interval),
        "OPENCOLLAB_REMOTE_ROOT": str(remote_root),
        "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
    })
    cmd = [
        str(remote_repo / "scripts" / "run_swe_v2_one_from_fifo.sh"),
        task,
        image,
        str(fifo),
        str(run_dir),
    ]
    with open_locked_append(log_path) as log:
        log.write(("\n===== generation start " + now() + " =====\n").encode())
        spawn_signal_state = block_spawn_signals()
        try:
            proc = subprocess.Popen(cmd, cwd=str(remote_root), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_fifo(fifo)
            return {"status": "generation_start_failed", "task": task, "details": str(exc), "log": str(log_path), "start_state": state}
        except BaseException:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_fifo(fifo)
            raise
        ACTIVE_CHILD_PGIDS.add(proc.pid)
        cleanup_quiesced = True
        try:
            restore_spawn_signals(spawn_signal_state)
            fifo_write = write_fifo_with_timeout(fifo, token + "\n")
            if not fifo_write.get("ok"):
                cleanup_quiesced = terminate_process_group_bounded(proc)
                cleanup_fifo(fifo)
                if not cleanup_quiesced:
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "details": fifo_write,
                        "log": str(log_path),
                    }
                return {"status": "fifo_write_failed", "task": task, "details": fifo_write, "log": str(log_path)}
            try:
                returncode = proc.wait(timeout=task_wall_timeout)
                cleanup_quiesced = ensure_process_group_quiesced_after_wait(proc)
                if not cleanup_quiesced:
                    cleanup_fifo(fifo)
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "details": "generator leader exited with residual process-group descendants",
                        "log": str(log_path),
                        "start_state": state,
                    }
            except subprocess.TimeoutExpired:
                log.write(("\nouter generation timeout after " + str(task_wall_timeout) + "s\n").encode())
                cleanup_quiesced = terminate_process_group_bounded(proc)
                if not cleanup_quiesced:
                    cleanup_fifo(fifo)
                    return {
                        "status": "technical_generation_cleanup_failed",
                        "task": task,
                        "returncode": PROCESS_CLEANUP_FAILED_EXIT_CODE,
                        "log": str(log_path),
                        "start_state": state,
                    }
                done, prediction, metric, pairing = generation_done(run_dir, task)
                cleanup_fifo(fifo)
                if done:
                    return generation_done_result(
                        task,
                        prediction,
                        metric,
                        pairing,
                        returncode=124,
                        log=str(log_path),
                        start_state=state,
                        timed_out=True,
                    )
                return {"status": "generation_timeout", "task": task, "returncode": 124, "log": str(log_path), "start_state": state}
        except BaseException:
            cleanup_quiesced = False
            try:
                cleanup_quiesced = terminate_process_group_bounded(proc)
            except BaseException:
                pass
            cleanup_fifo(fifo)
            raise
        finally:
            if cleanup_quiesced:
                ACTIVE_CHILD_PGIDS.discard(proc.pid)
    cleanup_fifo(fifo)
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(
            task,
            prediction,
            metric,
            pairing,
            returncode=returncode,
            log=str(log_path),
            start_state=state,
        )
    return {
        "status": "generation_failed",
        "task": task,
        "returncode": returncode,
        "log": str(log_path),
        "pairing": pairing,
        "patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
        "start_state": state,
    }


def eval_summary_matches_prediction(summary, prediction, task):
    if not isinstance(summary, dict) or summary.get("status") != "done":
        return False
    if not eval_model_patch(prediction).strip():
        return False
    if summary.get("task") and summary.get("task") != task:
        return False
    current_sha = row_patch_sha(prediction)
    previous_sha = str(summary.get("patch_sha256") or "")
    if not patch_sha_matches(previous_sha, current_sha):
        return False
    current_record = row_record_id(prediction)
    previous_record = str(summary.get("record_id") or "")
    if current_record and previous_record and current_record != previous_record:
        return False
    if current_record and not previous_record:
        return False
    return True


def eval_log_has_infra_failure(exit_status, log_text):
    if exit_status in {124, 126, 127}:
        return True
    if exit_status == 0:
        return False
    patterns = (
        r"\bconnectionrefusederror\b",
        r"\bconnection refused\b",
        r"\btemporary failure in name resolution\b",
        r"\bname or service not known\b",
        r"\bnetwork is unreachable\b",
        r"\bno space left on device\b",
        r"\bcannot connect to the docker daemon\b",
        r"\b(?:redis|mongodb?|postgres(?:ql)?|mysql|database)\b.{0,100}"
        r"\b(?:unavailable|refused|failed to connect|not running|timed out)\b",
    )
    for raw_line in str(log_text or "").splitlines():
        line = raw_line.lower()
        if "assertionerror" in line:
            continue
        if any(re.search(pattern, line) for pattern in patterns):
            return True
    return False


def cleanup_eval_container(cidfile, marker_path, container_name):
    try:
        cid = cidfile.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        cid = ""
    references = []
    if re.fullmatch(r"[0-9a-fA-F]{12,64}", cid):
        references.append(("cid", cid))
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(container_name or "")):
        references.append(("name", container_name))
    attempts = []
    for kind, reference in references:
        remove_result = run(["docker", "rm", "-f", reference], timeout=60)
        inspect_result = run(
            ["docker", "inspect", "--type", "container", reference],
            timeout=30,
        )
        inspect_details = str(
            inspect_result.get("stderr") or inspect_result.get("stdout") or ""
        )
        absent = bool(
            inspect_result.get("returncode") != 0
            and (
                "no such container" in inspect_details.lower()
                or "no such object" in inspect_details.lower()
            )
        )
        attempts.append({
            "kind": kind,
            "reference": reference,
            "remove_returncode": remove_result.get("returncode"),
            "remove_details": str(
                remove_result.get("stderr") or remove_result.get("stdout") or ""
            )[-1000:],
            "inspect_returncode": inspect_result.get("returncode"),
            "inspect_details": inspect_details[-1000:],
            "absent": absent,
        })
    if references and all(attempt.get("absent") for attempt in attempts):
        cidfile.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        return {
            "ok": True,
            "status": "all_references_absent",
            "attempts": attempts,
        }
    return {
        "ok": False,
        "status": "remove_failed",
        "attempts": attempts,
        "marker_path": str(marker_path),
        "cidfile": str(cidfile),
    }


def eval_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    eval_dir = run_dir / "official_eval_v1_prolite26_35_20260707"
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if not done:
        if prediction is not None and metric is not None:
            original_model_patch = prediction_patch(prediction)
            model_patch = eval_model_patch(prediction)
            status = workflow_status(metric)
            if original_model_patch.strip() and not model_patch.strip() and status in {"done", "done_with_timeout_patch"}:
                summary = {
                    "status": "empty_eval_patch_invalid",
                    "task": task,
                    "resolved": False,
                    "patch_sha256": row_patch_sha(prediction),
                    "record_id": row_record_id(prediction),
                    "model_patch_chars": len(original_model_patch),
                    "eval_model_patch_chars": 0,
                    "technical_reasons": ["empty_eval_patch_after_filter"],
                    "pairing": pairing,
                }
                write_json(summary_path, summary)
                return {"status": "empty_eval_patch_invalid", "task": task, "summary": summary}
        return {"status": "skipped_no_generation_patch", "task": task, "pairing": pairing}
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    if not fail_to_pass:
        summary = {
            "status": "blocked_missing_eval_spec",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "technical_reasons": ["missing_fail_to_pass"],
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {"status": "blocked_missing_eval_spec", "task": task, "summary": summary}
    previous = load_json(summary_path)
    if eval_summary_matches_prediction(previous, prediction, task):
        return {"status": "eval_done", "task": task, "summary": previous, "report_path": str(report_path)}
    if dry_run:
        return {"status": "would_eval", "task": task}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_eval_image", "task": task, "image_status": image_status}
    input_dir = eval_dir / "input"
    output_dir = report_path.parent
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    original_model_patch = prediction_patch(prediction)
    model_patch = eval_model_patch(prediction)
    test_patch = str(row.get("test_patch") or "")
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    f2p_cmd = prolite_test_command(row, fail_to_pass)
    p2p_cmd = prolite_test_command(row, pass_to_pass)
    service_bootstrap = prolite_service_bootstrap(row)
    atomic_write_bytes(input_dir / "model.patch", model_patch.encode("utf-8"))
    atomic_write_bytes(input_dir / "test.patch", test_patch.encode("utf-8"))
    atomic_write_bytes(
        input_dir / "service_bootstrap.sh",
        service_bootstrap.encode("utf-8"),
    )
    atomic_write_bytes(
        input_dir / "before_repo.sh",
        str(row.get("before_repo_set_cmd") or "").encode("utf-8"),
    )
    inner = f"""#!/usr/bin/env bash
set +e
cd /app 2>/dev/null || cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || cd /
export PATH="/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:/root/go/bin:/usr/local/node/bin:/opt/node/bin:/root/.local/share/pnpm:/root/.npm-global/bin:/app/node_modules/.bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1 && command -v corepack >/dev/null 2>&1; then
  corepack enable >/tmp/prolite_corepack.log 2>&1 || true
fi
bash /eval_input/service_bootstrap.sh > /eval_output/service_bootstrap.log 2>&1
echo "$?" > /eval_output/service_bootstrap.exit
bash /eval_input/before_repo.sh > /eval_output/before_repo.log 2>&1
echo "$?" > /eval_output/before_repo.exit
model_status=0
if [ -s /eval_input/model.patch ]; then
  git apply --whitespace=nowarn /eval_input/model.patch > /eval_output/model_patch.log 2>&1
  model_status=$?
  if [ "$model_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/model.patch >> /eval_output/model_patch.log 2>&1
    model_status=$?
  fi
fi
echo "$model_status" > /eval_output/model_patch.exit
test_status=0
if [ "$model_status" -eq 0 ] && [ -s /eval_input/test.patch ]; then
  git apply --whitespace=nowarn /eval_input/test.patch > /eval_output/test_patch.log 2>&1
  test_status=$?
  if [ "$test_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/test.patch >> /eval_output/test_patch.log 2>&1
    test_status=$?
  fi
fi
echo "$test_status" > /eval_output/test_patch.exit
if [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
  echo {shlex.quote(f2p_cmd)} > /eval_output/f2p.command
  bash -c {shlex.quote(f2p_cmd)} > /eval_output/f2p.log 2>&1
  echo "$?" > /eval_output/f2p.exit
  echo {shlex.quote(p2p_cmd)} > /eval_output/p2p.command
  bash -c {shlex.quote(p2p_cmd)} > /eval_output/p2p.log 2>&1
  echo "$?" > /eval_output/p2p.exit
else
  echo 99 > /eval_output/f2p.exit
  echo 99 > /eval_output/p2p.exit
fi
exit 0
"""
    script_path = input_dir / "run_prolite_direct_eval.sh"
    atomic_write_bytes(script_path, inner.encode("utf-8"))
    script_path.chmod(0o755)
    command_log = eval_dir / "command.log"
    cidfile = eval_dir / "container.cid"
    marker_path = eval_dir / "container.marker.json"
    previous_marker = load_json(marker_path)
    if isinstance(previous_marker, dict):
        previous_name = str(previous_marker.get("container_name") or "")
        stale_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            previous_name,
        )
        if not stale_cleanup.get("ok"):
            summary = {
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    elif marker_path.exists() or cidfile.exists():
        stale_cleanup = cleanup_eval_container(cidfile, marker_path, "")
        if not stale_cleanup.get("ok"):
            summary = {
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    container_name = "opencollab-prolite-" + hashlib.sha256(
        f"{base_run_dir}:{task}:{os.getpid()}:{time.time_ns()}".encode()
    ).hexdigest()[:24]
    cidfile.unlink(missing_ok=True)
    write_json(marker_path, {
        "schema": "opencollab.prolite_eval_container.v1",
        "task": task,
        "container_name": container_name,
        "cidfile": str(cidfile),
        "created_at": now(),
    })
    docker_cmd = [
        "timeout",
        str(eval_timeout),
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/bash",
        "--cidfile",
        str(cidfile),
        "-v",
        f"{input_dir}:/eval_input:ro",
        "-v",
        f"{output_dir}:/eval_output",
        image,
        "/eval_input/run_prolite_direct_eval.sh",
    ]
    cleanup_quiesced = True
    with open_locked_append(command_log) as log:
        log.write(("\n===== eval start " + now() + " =====\n").encode())
        spawn_signal_state = block_spawn_signals()
        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_eval_container(cidfile, marker_path, container_name)
            log.write((f"failed to start eval container: {exc}\n").encode())
            docker_exit = 127
        except BaseException:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                cleanup_eval_container(cidfile, marker_path, container_name)
            raise
        else:
            ACTIVE_CHILD_PGIDS.add(proc.pid)
            try:
                try:
                    restore_spawn_signals(spawn_signal_state)
                    docker_exit = proc.wait(timeout=eval_timeout + 120)
                    cleanup_quiesced = ensure_process_group_quiesced_after_wait(proc)
                    if not cleanup_quiesced:
                        docker_exit = PROCESS_CLEANUP_FAILED_EXIT_CODE
                except subprocess.TimeoutExpired:
                    log.write((f"outer eval timeout after {eval_timeout + 120}s\n").encode())
                    cleanup_quiesced = terminate_process_group_bounded(proc)
                    docker_exit = (
                        124 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                    )
                except BaseException:
                    cleanup_quiesced = False
                    try:
                        cleanup_quiesced = terminate_process_group_bounded(proc)
                    except BaseException:
                        pass
                    try:
                        cleanup_eval_container(
                            cidfile,
                            marker_path,
                            container_name,
                        )
                    except BaseException:
                        pass
                    raise
            finally:
                if cleanup_quiesced:
                    ACTIVE_CHILD_PGIDS.discard(proc.pid)

    if docker_exit != 0:
        container_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            container_name,
        )
    else:
        cidfile.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        container_cleanup = {"ok": True, "status": "not_needed"}

    output_artifact_errors = []

    def read_exit(name, default=99):
        path = output_dir / name
        try:
            with open_regular_binary(path) as handle:
                opened = os.fstat(handle.fileno())
                if opened.st_size > MAX_EXIT_STATUS_BYTES:
                    raise RecordInputLimitError(
                        f"exit status exceeds byte limit: {path}"
                    )
                raw = handle.read(MAX_EXIT_STATUS_BYTES + 1)
            text = raw.decode("ascii").strip()
            if not re.fullmatch(r"-?[0-9]+", text):
                raise RecordInputFormatError(f"invalid exit status: {path}")
            return int(text)
        except FileNotFoundError:
            output_artifact_errors.append(f"missing:{name}")
            return default
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            output_artifact_errors.append(f"unsafe:{name}:{type(exc).__name__}")
            return default

    def read_text(name, limit=4000):
        try:
            return read_tail_text(output_dir / name, limit)
        except OSError as exc:
            output_artifact_errors.append(f"unsafe:{name}:{type(exc).__name__}")
            return ""

    service_status = read_exit("service_bootstrap.exit", 0)
    before_status = read_exit("before_repo.exit")
    model_status = read_exit("model_patch.exit")
    test_status = read_exit("test_patch.exit")
    f2p_status = read_exit("f2p.exit")
    p2p_status = read_exit("p2p.exit", 0)
    f2p_log_tail = read_text("f2p.log")
    p2p_log_tail = read_text("p2p.log")
    technical_reasons = []
    if output_artifact_errors:
        technical_reasons.append("unsafe_or_missing_output_artifact")
    if not _is_runnable_test_command(f2p_cmd):
        # No derivable FAIL_TO_PASS command: we cannot prove the fix, so this is a
        # technical failure (resolved stays False) — never a silent pass.
        technical_reasons.append("no_fail_to_pass_command")
    if docker_exit != 0:
        technical_reasons.append("docker_exit")
    if not cleanup_quiesced:
        technical_reasons.append("process_cleanup")
    if not container_cleanup.get("ok"):
        technical_reasons.append("container_cleanup")
    if service_status != 0:
        technical_reasons.append("service_bootstrap")
    if before_status != 0:
        technical_reasons.append("before_repo")
    if model_status != 0:
        technical_reasons.append("model_patch")
    if test_status != 0:
        technical_reasons.append("test_patch")
    if eval_log_has_infra_failure(f2p_status, f2p_log_tail):
        technical_reasons.append("fail_to_pass_infra")
    if eval_log_has_infra_failure(p2p_status, p2p_log_tail):
        technical_reasons.append("pass_to_pass_infra")
    technical_error = bool(technical_reasons)
    resolved = bool(not technical_error and f2p_status == 0 and p2p_status == 0)
    summary_status = "technical_eval_failed" if technical_error else "done"
    report = {
        "schema": "opencollab.prolite_direct_eval.v1",
        "status": summary_status,
        "instance_id": task,
        "resolved": resolved,
        "patch_successfully_applied": model_status == 0,
        "error": bool(technical_error),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "tests_status": {
            "service_bootstrap_status": service_status,
            "before_repo_status": before_status,
            "model_patch_status": model_status,
            "test_patch_status": test_status,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": p2p_status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "f2p_command": read_text("f2p.command", 1000),
            "p2p_command": read_text("p2p.command", 1000),
            "service_bootstrap_log_tail": read_text("service_bootstrap.log"),
            "f2p_log_tail": f2p_log_tail,
            "p2p_log_tail": p2p_log_tail,
            "model_patch_log_tail": read_text("model_patch.log"),
            "test_patch_log_tail": read_text("test_patch.log"),
        },
    }
    write_json(report_path, {task: report})
    summary = {
        "status": summary_status,
        "task": task,
        "resolved": resolved,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "report_path": str(report_path),
        "command_log": str(command_log),
        "tests_status": report["tests_status"],
    }
    write_json(summary_path, summary)
    return {"status": "eval_done" if not technical_error else "technical_eval_failed", "task": task, "summary": summary, "report_path": str(report_path)}


def write_markdown(summary):
    lines = [
        f"# SWE G1.1 Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- tasks: `{summary['counts']['tasks']}`",
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
        f"- resolved: `{summary['counts']['resolved']}`",
        f"- unresolved: `{summary['counts']['unresolved']}`",
        f"- technical_failed: `{summary['counts']['technical_failed']}`",
        "",
        "| idx | task | generation | eval | resolved | patch | report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        report = row.get("eval", {}).get("report_path") or ""
        patch_sha = (
            row.get("generation", {}).get("patch_sha256")
            or row.get("eval", {}).get("summary", {}).get("patch_sha256")
            or ""
        )
        lines.append(
            "| {idx} | `{task}` | `{gen}` | `{ev}` | `{resolved}` | `{patch}` | `{report}` |".format(
                idx=row["index"],
                task=row["task"],
                gen=row.get("generation", {}).get("status", ""),
                ev=row.get("eval", {}).get("status", ""),
                resolved=row.get("eval", {}).get("summary", {}).get("resolved", ""),
                patch=patch_sha[:12],
                report=report,
            )
        )
    summary["markdown"] = "\n".join(lines) + "\n"


def main():
    config_errors = validate_runner_config()
    if config_errors:
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "invalid_config",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "config_errors": config_errors,
            "counts": {"tasks": 0, "generation_done": 0, "eval_done": 0, "resolved": 0, "unresolved": 0, "technical_failed": 1},
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    preflight = {
        "dataset_exists": dataset_path.exists(),
        "remote_root_exists": remote_root.exists(),
        "remote_repo_exists": remote_repo.exists(),
        "proxy_health": http_health(remote_proxy_base_url + "/healthz", timeout=45),
    }
    if not all([preflight["dataset_exists"], preflight["remote_root_exists"], preflight["remote_repo_exists"], preflight["proxy_health"].get("ok")]):
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "preflight_failed",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "preflight": preflight,
            "counts": {"tasks": 0, "generation_done": 0, "eval_done": 0, "resolved": 0, "unresolved": 0, "technical_failed": 1},
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    selected = load_dataset(start_index, limit)
    base_run_dir.mkdir(parents=True, exist_ok=True)
    result_rows = []
    for offset, row in enumerate(selected, start_index):
        task = row["instance_id"]
        gen = generation_for_task(row)
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "generation", "task": task, "result": gen})
        if dry_run and gen.get("status") in {"would_generate", "generation_done"}:
            ev = {"status": "would_eval", "task": task}
        elif gen.get("status") == "generation_done":
            ev = eval_for_task(row)
        else:
            ev = {
                "status": "skipped_generation_not_ready",
                "task": task,
                "generation_status": gen.get("status"),
                "reason": "generation_not_ready",
            }
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "eval", "task": task, "result": ev})
        result_rows.append({"index": offset, "task": task, "generation": gen, "eval": ev})
    generation_ok_statuses = {"generation_done"}
    eval_ok_statuses = {"eval_done"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
        "resolved": sum(1 for row in result_rows if row["eval"].get("summary", {}).get("resolved") is True),
        "unresolved": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done" and row["eval"].get("summary", {}).get("resolved") is False),
        "technical_failed": sum(
            1
            for row in result_rows
            if row["generation"].get("status") not in generation_ok_statuses
            or row["eval"].get("status") not in eval_ok_statuses
        ),
    }
    status = "done" if counts["technical_failed"] == 0 else "done_with_technical_failures"
    if dry_run and counts["technical_failed"] == 0:
        status = "dry_run"
    summary = {
        "schema": "opencollab.swe_g11_prolite_runner.v1",
        "status": status,
        "generated_at": now(),
        "slice": slice_label(),
        "base_run_dir": str(base_run_dir),
        "remote_runtime_repo": str(remote_repo),
        "workflow": workflow,
        "model_name": model_name,
        "preflight": preflight,
        "counts": counts,
        "rows": result_rows,
    }
    write_markdown(summary)
    write_json(base_run_dir / "summary.json", summary)
    atomic_write_bytes(
        base_run_dir / "summary.md",
        summary["markdown"].encode("utf-8"),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if counts["technical_failed"] == 0 else 1


def validate_runner_config():
    errors = []
    if start_index < 1:
        errors.append("start_index must be >= 1")
    if limit <= 0:
        errors.append("limit must be > 0")
    if limit > MAX_TASKS_PER_RUN:
        errors.append(f"limit must be <= {MAX_TASKS_PER_RUN}")
    if max_task_starts < 0:
        errors.append("max_task_starts must be >= 0")
    return errors


raise SystemExit(main())
'''


def _redacted(text: str) -> str:
    text = re.sub(r"(GLM_PROXY_CLIENT_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_AUTH_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(OPENCOLLAB_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}", r"\1[redacted]", text)
    return text


def run_checked(command: list[str], *, timeout: int = 120, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"{command[0]} exited {result.returncode}"))
    return result


def _read_bounded_regular_text(path: Path, *, max_bytes: int) -> str:
    path = path.expanduser()
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise RuntimeError(f"input must be a bounded regular file: {path}")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(f"input changed while opening: {path}")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RuntimeError(f"input exceeds {max_bytes} bytes: {path}")
    return raw.decode("utf-8")


def load_shell_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = _read_bounded_regular_text(path, max_bytes=MAX_PROXY_ENV_BYTES)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        parsed = shlex.split(value, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def token_from_values(values: dict[str, str]) -> str:
    for name in ("GLM_PROXY_CLIENT_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENCOLLAB_API_KEY"):
        value = values.get(name)
        if value:
            return value
    return ""


def token_from_env_file(path: Path) -> str:
    try:
        return token_from_values(load_shell_env(path))
    except FileNotFoundError:
        return ""


def proxy_env_file_from_ps(ps_text: str) -> Path | None:
    try:
        parts = shlex.split(ps_text)
    except ValueError:
        return None
    for index, part in enumerate(parts):
        if part == "--env-file" and index + 1 < len(parts):
            return Path(parts[index + 1])
        if part.startswith("--env-file="):
            return Path(part.split("=", 1)[1])
    return None


def get_proxy_token(proxy_env_file: Path) -> str:
    token = token_from_values(dict(os.environ))
    if token:
        return token
    token = token_from_env_file(proxy_env_file)
    if token:
        return token
    try:
        pids = subprocess.check_output(
            [
                "pgrep",
                "-f",
                "opencollab_glm_anthropic_proxy.py|glm_anthropic_proxy.py",
            ],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        ).split()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "timed out while locating the glm proxy process"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("glm proxy process not found") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to locate the glm proxy process: {exc}") from exc
    if not pids:
        raise RuntimeError("glm proxy process not found")
    try:
        ps = subprocess.check_output(
            ["ps", "eww", "-p", pids[0]],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while reading the glm proxy environment") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(f"failed to read the glm proxy environment: {exc}") from exc
    env_path = proxy_env_file_from_ps(ps)
    if env_path:
        token = token_from_env_file(env_path)
        if token:
            return token
    match = re.search(r"GLM_PROXY_CLIENT_TOKEN=(\S+)", ps)
    if not match:
        raise RuntimeError("proxy token not found in environment, proxy env file, or proxy process")
    return match.group(1)


def url_with_healthz(base_url: str) -> str:
    return base_url.rstrip("/") + "/healthz"


def local_http_ok(base_url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def remote_http_ok(*, ssh_command: list[str], host: str, base_url: str, timeout: int = 10) -> bool:
    probe = (
        "import sys,urllib.request;"
        "urllib.request.urlopen(sys.argv[1], timeout="
        + str(timeout)
        + ").read()"
    )
    try:
        result = subprocess.run(
            [*ssh_command, host, "python3 -c " + shlex.quote(probe) + " " + shlex.quote(url_with_healthz(base_url))],
            text=True,
            capture_output=True,
            timeout=max(REMOTE_HEALTH_SSH_TIMEOUT_FLOOR, timeout + 8),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def loopback_port(base_url: str, *, default: int) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    return int(parsed.port or default)


def loopback_url_with_port(base_url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if host == "::1":
        netloc = f"[::1]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def remote_forward_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "remote port forwarding failed" in lowered
        or "address already in use" in lowered
        or "cannot listen to port" in lowered
    )


def stop_remote_proxy_tunnel(proc: subprocess.Popen[str]) -> bool:
    return terminate_local_process_group(proc)


def cleanup_remote_proxy_tunnels() -> None:
    for proc in list(REMOTE_PROXY_TUNNELS):
        try:
            cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        except BaseException:
            cleanup_quiesced = False
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)


atexit.register(cleanup_remote_proxy_tunnels)


def start_remote_proxy_tunnel(command: list[str]) -> tuple[subprocess.Popen[str] | None, str]:
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    REMOTE_PROXY_TUNNELS.append(proc)
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        time.sleep(0.2)
        if proc.poll() is not None:
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_quiesced = terminate_local_process_group(proc)
                if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
                    REMOTE_PROXY_TUNNELS.remove(proc)
                return None, "ssh tunnel output drain timed out"
            cleanup_quiesced = _ensure_local_process_group_quiesced_after_wait(proc)
            if cleanup_quiesced:
                REMOTE_PROXY_TUNNELS.remove(proc)
            else:
                return None, "ssh tunnel leader exited with residual process-group descendants that could not be cleaned"
            message = _redacted(
                stderr or stdout or f"{command[0]} exited {proc.returncode}"
            )
            return None, message
        return proc, ""
    except BaseException:
        cleanup_quiesced = False
        try:
            cleanup_quiesced = terminate_local_process_group(proc)
        except BaseException:
            pass
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        raise


def ensure_remote_proxy(
    *,
    ssh_command: list[str],
    host: str,
    local_proxy_base_url: str,
    remote_proxy_base_url: str,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    if remote_http_ok(ssh_command=ssh_command, host=host, base_url=remote_proxy_base_url):
        return {"status": "already_healthy", "remote_proxy_base_url": remote_proxy_base_url}
    if not local_http_ok(local_proxy_base_url):
        raise RuntimeError(f"local proxy health check failed: {url_with_healthz(local_proxy_base_url)}")
    local_port = loopback_port(local_proxy_base_url, default=8878)
    remote_port = loopback_port(remote_proxy_base_url, default=18788)
    attempts: list[str] = []
    for candidate_port in range(remote_port, remote_port + 21):
        candidate_base_url = loopback_url_with_port(remote_proxy_base_url, candidate_port)
        forward = f"127.0.0.1:{candidate_port}:127.0.0.1:{local_port}"
        command = [
            *ssh_command,
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            forward,
            host,
        ]
        proc, message = start_remote_proxy_tunnel(command)
        if proc is None:
            attempts.append(f"{candidate_port}: {message}")
            if remote_forward_port_conflict(message):
                if remote_http_ok(
                    ssh_command=ssh_command,
                    host=host,
                    base_url=candidate_base_url,
                    timeout=2,
                ):
                    return {
                        "status": "already_healthy",
                        "remote_proxy_base_url": candidate_base_url,
                        "selected_remote_port": candidate_port,
                    }
                continue
            raise RuntimeError(message)
        for _ in range(6):
            if remote_http_ok(ssh_command=ssh_command, host=host, base_url=candidate_base_url, timeout=2):
                return {
                    "status": "started" if candidate_port == remote_port else "started_fallback_port",
                    "local_proxy_base_url": local_proxy_base_url,
                    "remote_proxy_base_url": candidate_base_url,
                    "forward": forward,
                    "selected_remote_port": candidate_port,
                }
            time.sleep(0.5)
        cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        if not cleanup_quiesced:
            raise RuntimeError(
                f"remote proxy tunnel on port {candidate_port} did not stop"
            )
        attempts.append(f"{candidate_port}: tunnel started but health check failed")
    detail = "; ".join(attempts[-5:])
    raise RuntimeError(
        f"remote proxy tunnel did not become healthy near port {remote_port}: {detail}"
    )


def sync_runtime(*, ssh_command: list[str], host: str, remote_runtime_repo: str) -> dict[str, Any]:
    synced: list[str] = []
    synced_dirs: list[str] = []
    ssh_part = " ".join(shlex.quote(part) for part in ssh_command)
    with tempfile.TemporaryDirectory(prefix="swe-v1-runtime-") as tmp_dir:
        archive_path = Path(tmp_dir) / "runtime.tgz"

        def archive_filter(tar_info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(tar_info.name).parts
            if "__pycache__" in parts or tar_info.name.endswith((".pyc", ".pyo")):
                return None
            return tar_info

        with tarfile.open(archive_path, "w:gz") as archive:
            for rel in SYNC_FILES:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced.append(rel)
            for rel in SYNC_DIRS:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced_dirs.append(rel)
        run_checked([*ssh_command, host, "mkdir -p " + shlex.quote(remote_runtime_repo)], timeout=60)
        remote_archive = remote_runtime_repo.rstrip("/") + "/runtime.tgz"
        run_checked(["rsync", "-az", "-e", ssh_part, str(archive_path), f"{host}:{remote_archive}"], timeout=300)
        run_checked([*ssh_command, host, "tar -xzf " + shlex.quote(remote_archive) + " -C " + shlex.quote(remote_runtime_repo)], timeout=300)
    sh_files = [rel for rel in synced if rel.endswith(".sh")]
    if sh_files:
        run_checked(
            [*ssh_command, host, "cd " + shlex.quote(remote_runtime_repo) + " && chmod +x " + " ".join(shlex.quote(rel) for rel in sh_files)],
            timeout=60,
        )
    compile_targets = [rel for rel in ("scripts", "swebench", "workflows", *SYNC_DIRS) if rel in synced_dirs or any(item == rel or item.startswith(rel + "/") for item in synced)]
    if compile_targets:
        run_checked(
            [*ssh_command, host, "cd " + shlex.quote(remote_runtime_repo) + " && python3 -m compileall -q " + " ".join(shlex.quote(rel) for rel in compile_targets)],
            timeout=180,
        )
    return {"remote_runtime_repo": remote_runtime_repo, "synced": synced, "synced_dirs": synced_dirs, "compile_targets": compile_targets}


def configure_run_paths(args: argparse.Namespace) -> None:
    if not args.run_id:
        args.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    if not args.base_run_dir:
        args.base_run_dir = DEFAULT_BASE_RUN_DIR_PREFIX + "_" + args.run_id
    if not args.remote_runtime_repo:
        args.remote_runtime_repo = str(Path(args.base_run_dir) / "_runtime" / "repo")


def validate_run_id(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("run_id must be one non-empty path component")
    if Path(value).is_absolute() or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise ValueError("run_id must be one safe path component")
    if len(value.encode("utf-8")) > 240:
        raise ValueError("run_id exceeds 240 UTF-8 bytes")
    return value


def terminate_remote_run(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    timeout: int = 30,
) -> dict[str, Any]:
    cleanup = r'''
import json
import os
import pathlib
import signal
import shlex
import stat
import subprocess
import sys
import time

base = pathlib.Path(sys.argv[1])
me = os.getpid()
parent = os.getppid()


def send_pid(pid, sig):
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def send_pgid(pgid, sig):
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def process_start_identity(pid):
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        if len(remainder) > 19 and remainder[19].isdigit():
            return f"proc:{remainder[19]}"
    except (OSError, IndexError) as exc:
        scan_errors.append(repr(exc))
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        scan_errors.append(repr(exc))
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""


def read_owner(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
        raise RuntimeError("runner owner is not a bounded regular file")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(fd)
        payload = json.loads(os.read(fd, 4097).decode("utf-8"))
        current = path.lstat()
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        or not isinstance(payload, dict)
        or payload.get("schema") != "opencollab.prolite_runner_owner.v1"
    ):
        raise RuntimeError("runner owner identity is invalid")
    return payload


def scan(owner_nonce):
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,pgid=,args="],
            text=True,
            timeout=5,
        )
    except Exception as exc:
        scan_errors.append(repr(exc))
        return []
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        if pid in {me, parent}:
            continue
        try:
            tokens = shlex.split(args)
        except ValueError:
            continue
        if owner_nonce in tokens:
            rows.append((pid, pgid, args))
    return rows


killed = []
scan_errors = []
containers = []
for pattern in ("container.id", "container.cid"):
    for marker in base.rglob(pattern):
        try:
            cid = marker.read_text(encoding="utf-8", errors="replace").strip()
            if cid:
                containers.append(cid)
        except Exception:
            pass
for marker in base.rglob("container.marker.json"):
    try:
        payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("container_name"):
            containers.append(str(payload["container_name"]))
    except Exception:
        pass
runner_pid_path = base / "runner.pid"
try:
    owner = read_owner(runner_pid_path)
except FileNotFoundError:
    owner = None
    scan_errors.append("runner owner record is missing")
except Exception as exc:
    owner = None
    scan_errors.append(repr(exc))

owner_nonce = str((owner or {}).get("owner_nonce") or "")
try:
    runner_pid = int((owner or {}).get("pid") or 0)
except (TypeError, ValueError):
    runner_pid = 0
expected_start = str((owner or {}).get("start_identity") or "")
current_start = process_start_identity(runner_pid) if runner_pid > 1 else ""
owner_matches = bool(
    runner_pid > 1
    and runner_pid not in {me, parent}
    and expected_start
    and current_start == expected_start
)
if runner_pid > 1 and current_start and not owner_matches:
    scan_errors.append("runner PID/start identity mismatch")
if owner_matches:
    if send_pid(runner_pid, signal.SIGTERM):
        killed.append({"pid": runner_pid, "signal": "TERM"})

for sig_name, sig_value, delay in (("TERM", signal.SIGTERM, 2.0), ("KILL", signal.SIGKILL, 0.0)):
    for pid, pgid, _args in scan(owner_nonce) if owner_nonce else []:
        if send_pid(pid, sig_value):
            killed.append({"pid": pid, "pgid": pgid, "signal": sig_name})
    if delay:
        time.sleep(delay)

residual_processes = scan(owner_nonce) if owner_nonce else []

container_results = []
for cid in sorted(set(containers)):
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", cid],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        container_results.append(
            {
                "cid": cid,
                "returncode": result.returncode,
                "stdout": result.stdout.strip()[:200],
                "stderr": result.stderr.strip()[:200],
            }
        )
    except Exception as exc:
        container_results.append({"cid": cid, "error": repr(exc)})

containers_ok = all(
    item.get("returncode") == 0
    or "no such container" in str(item.get("stderr") or "").lower()
    for item in container_results
)
cleanup_ok = not scan_errors and not residual_processes and containers_ok
status = "done" if cleanup_ok else "technical_cleanup_failed"
print(json.dumps({
    "ok": cleanup_ok,
    "status": status,
    "killed": killed,
    "containers": container_results,
    "scan_errors": scan_errors,
    "residual_processes": [
        {"pid": pid, "pgid": pgid, "args": args[:500]}
        for pid, pgid, args in residual_processes
    ],
}, ensure_ascii=False))
raise SystemExit(0 if cleanup_ok else 3)
'''
    result = subprocess.run(
        [*ssh_command, host, "python3 -c " + shlex.quote(cleanup) + " " + shlex.quote(base_run_dir)],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        detail = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = {"stdout": _redacted(result.stdout), "stderr": _redacted(result.stderr)}
    return {"returncode": result.returncode, "detail": detail}


def _wait_for_owned_local_cleanup(
    done: threading.Event,
    *,
    timeout: float,
) -> tuple[bool, BaseException | None]:
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


def _block_local_spawn_signals() -> dict[str, object]:
    state: dict[str, object] = {
        "previous": {},
        "pending": [],
        "restored": False,
    }

    def defer(signum: int, _frame: object) -> None:
        pending = state["pending"]
        if isinstance(pending, list) and signum not in pending:
            pending.append(signum)

    previous: dict[signal.Signals, Any] = {}
    state["previous"] = previous
    try:
        for signum in LOCAL_SPAWN_SIGNALS:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, defer)
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    return state


def _restore_local_spawn_signals(
    state: dict[str, object],
) -> None:
    if state.get("restored"):
        return
    previous = state.get("previous")
    if not isinstance(previous, dict):
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)
    state["restored"] = True
    pending = state.get("pending")
    for signum in pending if isinstance(pending, list) else []:
        handler = previous.get(signum, signal.SIG_DFL)
        if handler == signal.SIG_IGN:
            continue
        if handler == signal.SIG_DFL:
            os.kill(os.getpid(), signum)
        else:
            handler(signum, None)


class _BoundedTextTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = ""
        self.total_chars = 0
        self.lock = threading.Lock()

    def append(self, chunk: str) -> None:
        with self.lock:
            self.total_chars += len(chunk)
            self.value = (self.value + chunk)[-self.limit :]

    def render(self) -> str:
        with self.lock:
            if self.total_chars <= self.limit:
                return self.value
            omitted = self.total_chars - len(self.value)
            return f"[truncated {omitted} chars]\n{self.value}"


def _drain_text_stream(stream: Any, sink: _BoundedTextTail) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def _bounded_remote_communicate(
    proc: subprocess.Popen[str],
    input_text: str,
    *,
    timeout: float,
) -> tuple[str, str]:
    if (
        getattr(proc, "stdout", None) is None
        or getattr(proc, "stderr", None) is None
        or getattr(proc, "stdin", None) is None
    ):
        return proc.communicate(input_text, timeout=timeout)
    stdout_tail = _BoundedTextTail(MAX_REMOTE_OUTPUT_TAIL_CHARS)
    stderr_tail = _BoundedTextTail(MAX_REMOTE_OUTPUT_TAIL_CHARS)
    threads = [
        threading.Thread(
            target=_drain_text_stream,
            args=(proc.stdout, stdout_tail),
            name=f"prolite-stdout-{proc.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_text_stream,
            args=(proc.stderr, stderr_tail),
            name=f"prolite-stderr-{proc.pid}",
            daemon=True,
        ),
    ]
    setattr(proc, "_opencollab_bounded_drainers", threads)
    for thread in threads:
        thread.start()
    try:
        proc.stdin.write(input_text)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    proc.wait(timeout=timeout)
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("remote output drain did not reach EOF")
    return stdout_tail.render(), stderr_tail.render()


def _wait_or_communicate_local_process(
    proc: subprocess.Popen[str],
    *,
    timeout: float | None = None,
) -> None:
    if getattr(proc, "_opencollab_bounded_drainers", None) is not None:
        proc.wait(timeout=timeout)
    else:
        proc.communicate(timeout=timeout)


def _consume_local_process_exit(proc: subprocess.Popen[str]) -> None:
    try:
        _wait_or_communicate_local_process(proc)
    except BaseException:
        pass


def _schedule_local_process_exit_consumer(proc: subprocess.Popen[str]) -> None:
    threading.Thread(
        target=_consume_local_process_exit,
        args=(proc,),
        name=f"prolite-local-reap-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def _local_process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_local_process_group_exit(pgid: int, *, deadline: float) -> bool:
    while _local_process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _terminate_local_process_group_owned(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    pgid = proc.pid
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            _schedule_local_process_exit_consumer(proc)
            return False

    leader_reaped = False
    try:
        _wait_or_communicate_local_process(
            proc,
            timeout=max(0.0, term_deadline - time.monotonic()),
        )
        leader_reaped = True
    except ChildProcessError:
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass

    group_gone = _wait_for_local_process_group_exit(pgid, deadline=term_deadline)
    if leader_reaped and group_gone:
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    if not leader_reaped:
        try:
            _wait_or_communicate_local_process(
                proc,
                timeout=max(0.0, kill_deadline - time.monotonic()),
            )
            leader_reaped = True
        except ChildProcessError:
            leader_reaped = True
        except subprocess.TimeoutExpired:
            pass
    group_gone = _wait_for_local_process_group_exit(pgid, deadline=kill_deadline)
    if not leader_reaped:
        _schedule_local_process_exit_consumer(proc)
    return leader_reaped and group_gone


def terminate_local_process_group(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float = LOCAL_PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
) -> bool:
    """Terminate an SSH wrapper and drain its pipes without an unbounded wait."""
    state: dict[str, object] = {}
    done = threading.Event()

    def cleanup() -> None:
        try:
            state["reaped"] = _terminate_local_process_group_owned(
                proc,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
            )
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    cleanup_thread = threading.Thread(
        target=cleanup,
        name=f"prolite-local-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    cleanup_thread.start()
    completed, interruption = _wait_for_owned_local_cleanup(
        done,
        timeout=(
            term_timeout
            + kill_timeout
            + LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS
        ),
    )
    if completed and "reaped" in state:
        reaped = bool(state["reaped"])
    else:
        reaped = False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
    if interruption is not None:
        raise interruption
    return reaped


def _ensure_local_process_group_quiesced_after_wait(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float = LOCAL_PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
) -> bool:
    if not _local_process_group_exists(proc.pid):
        return True
    return terminate_local_process_group(
        proc,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def _cleanup_remote_execution(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    proc: subprocess.Popen[str],
) -> tuple[dict[str, Any], BaseException | None]:
    """Run remote and local cleanup under one caller-interrupt-resistant owner."""
    state: dict[str, object] = {}
    done = threading.Event()

    def cleanup() -> None:
        remote_state: dict[str, object] = {}
        remote_done = threading.Event()

        def cleanup_remote() -> None:
            try:
                remote_state["result"] = terminate_remote_run(
                    ssh_command=ssh_command,
                    host=host,
                    base_run_dir=base_run_dir,
                    timeout=int(REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS),
                )
            except BaseException as exc:
                remote_state["result"] = {
                    "returncode": 125,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                remote_done.set()

        try:
            threading.Thread(
                target=cleanup_remote,
                name=f"prolite-remote-command-cleanup-{getattr(proc, 'pid', 'unknown')}",
                daemon=True,
            ).start()
            try:
                local_quiesced = terminate_local_process_group(proc)
            except BaseException as exc:
                local_quiesced = False
                state["local_error"] = f"{type(exc).__name__}: {exc}"
            remote_completed = remote_done.wait(
                REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS + 1.0
            )
            remote = remote_state.get("result")
            if not remote_completed or not isinstance(remote, dict):
                remote = {
                    "returncode": 125,
                    "error": "remote cleanup exceeded its outer bound",
                }
            state["result"] = {
                "ok": remote.get("returncode") == 0 and local_quiesced,
                "remote": remote,
                "local_cleanup_quiesced": local_quiesced,
                "completed": True,
            }
        finally:
            done.set()

    threading.Thread(
        target=cleanup,
        name=f"prolite-remote-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()
    completed, interruption = _wait_for_owned_local_cleanup(
        done,
        timeout=(
            max(
                REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS + 1.0,
                LOCAL_PROCESS_TERM_GRACE_SECONDS
                + LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS
                + LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
            )
            + 1.0
        ),
    )
    if completed and isinstance(state.get("result"), dict):
        result = dict(state["result"])
        if "local_error" in state:
            result["local_error"] = state["local_error"]
        return result, interruption

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    _schedule_local_process_exit_consumer(proc)
    return {
        "ok": False,
        "remote": state.get("result", {}).get("remote")
        if isinstance(state.get("result"), dict)
        else {"returncode": 125, "error": "cleanup exceeded outer bound"},
        "local_cleanup_quiesced": False,
        "completed": False,
    }, interruption


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    ssh_command = shlex.split(args.ssh_command)
    proxy_summary = ensure_remote_proxy(
        ssh_command=ssh_command,
        host=args.host,
        local_proxy_base_url=args.local_proxy_base_url,
        remote_proxy_base_url=args.remote_proxy_base_url,
        enabled=not args.no_ensure_remote_proxy,
    )
    sync_summary = {} if args.no_sync_runtime else sync_runtime(
        ssh_command=ssh_command,
        host=args.host,
        remote_runtime_repo=args.remote_runtime_repo,
    )
    selected_remote_proxy_base_url = proxy_summary.get(
        "remote_proxy_base_url", args.remote_proxy_base_url
    )
    owner_nonce = uuid.uuid4().hex
    payload = {
        "token": get_proxy_token(args.proxy_env_file),
        "owner_nonce": owner_nonce,
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "base_run_dir": args.base_run_dir,
        "workflow": args.workflow,
        "model_name": args.model_name,
        "session_prefix": args.session_prefix,
        "remote_proxy_base_url": selected_remote_proxy_base_url,
        "start_index": args.start_index,
        "limit": args.limit,
        "budget": args.budget,
        "max_steps": args.max_steps,
        "swe_timeout": args.swe_timeout,
        "task_wall_timeout": args.task_wall_timeout,
        "eval_timeout": args.eval_timeout,
        "llm_timeout": args.llm_timeout,
        "checkpoint_interval": args.checkpoint_interval,
        "max_task_starts": args.max_task_starts,
        "dry_run": args.dry_run,
    }
    encoded = base64.b64encode(REMOTE_RUNNER.encode("utf-8")).decode("ascii")
    wrapper = "import base64; exec(base64.b64decode(%r).decode('utf-8'))" % encoded
    command = [
        *ssh_command,
        args.host,
        "python3 -c "
        + shlex.quote(wrapper)
        + " "
        + shlex.quote(owner_nonce),
    ]
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        stdout, stderr = _bounded_remote_communicate(
            proc,
            json.dumps(payload),
            timeout=args.total_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            proc=proc,
        )
        if interruption is not None:
            raise interruption
        raise RuntimeError(
            f"remote run timed out after {args.total_timeout}s; cleanup={cleanup}"
        ) from exc
    except BaseException:
        cleanup, _interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            proc=proc,
        )
        print(
            "remote execution aborted; cleanup requested: "
            + json.dumps(cleanup, ensure_ascii=False),
            file=sys.stderr,
        )
        raise
    if _local_process_group_exists(proc.pid):
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            proc=proc,
        )
        if interruption is not None:
            raise interruption
        if not cleanup.get("ok"):
            raise RuntimeError(
                "ssh leader exited with residual process-group descendants; "
                f"technical cleanup failure: {cleanup}"
            )
    result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    if result.returncode not in (0, 1, 2):
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"ssh exited {result.returncode}"))
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(_redacted(result.stdout[-4000:] or result.stderr[-4000:])) from exc
    summary["runtime_sync"] = sync_summary
    summary["remote_proxy"] = proxy_summary
    return summary


def _prepare_local_report_output(path: Path, payload: bytes) -> tuple[Path, os.stat_result | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"report destination must be regular or absent: {path}")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary, before


def _commit_local_report_output(
    path: Path,
    temporary: Path,
    before: os.stat_result | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if before is None:
        if current is not None:
            raise OSError(f"report destination appeared during write: {path}")
    elif (
        current is None
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise OSError(f"report destination changed during write: {path}")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.abspath(json_path) == os.path.abspath(md_path):
        raise ValueError("JSON and Markdown reports must use different paths")
    bundle_id = uuid.uuid4().hex
    bundled_summary = {**summary, "local_report_bundle_id": bundle_id}
    json_payload = (
        json.dumps(bundled_summary, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    markdown = markdown.rstrip("\n") + f"\n\n<!-- local_report_bundle_id:{bundle_id} -->\n"
    prepared: list[tuple[Path, Path, os.stat_result | None]] = []
    try:
        json_temp, json_before = _prepare_local_report_output(json_path, json_payload)
        prepared.append((json_path, json_temp, json_before))
        md_temp, md_before = _prepare_local_report_output(
            md_path,
            markdown.encode("utf-8"),
        )
        prepared.append((md_path, md_temp, md_before))
        # JSON is the commit marker: both complete files exist before either
        # destination changes, and JSON is published after Markdown.
        _commit_local_report_output(md_path, md_temp, md_before)
        _commit_local_report_output(json_path, json_temp, json_before)
    finally:
        for _path, temporary, _before in prepared:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run G1.1 validation-council on a SWE-batch-pro-lite slice and evaluate it.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--remote-runtime-repo", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--base-run-dir", default="")
    parser.add_argument("--start-index", type=int, default=26)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--workflow", default="validation-council-solve")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--session-prefix", default="swe_g11_pro35_16m")
    parser.add_argument("--remote-proxy-base-url", default="http://127.0.0.1:18788")
    parser.add_argument("--local-proxy-base-url", default=DEFAULT_LOCAL_PROXY_BASE_URL)
    parser.add_argument("--proxy-env-file", type=Path, default=DEFAULT_PROXY_ENV_FILE)
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--swe-timeout", type=int, default=14_400)
    parser.add_argument("--task-wall-timeout", type=int, default=15_300)
    parser.add_argument("--eval-timeout", type=int, default=7_200)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--checkpoint-interval", type=int, default=300)
    parser.add_argument("--max-task-starts", type=int, default=1)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.start_index < 1:
        parser.error("--start-index must be >= 1")
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.limit > MAX_TASKS_PER_RUN:
        parser.error(f"--limit must be <= {MAX_TASKS_PER_RUN}")
    if args.max_task_starts < 0:
        parser.error("--max-task-starts must be >= 0")
    positive_values = {
        "--budget": args.budget,
        "--max-steps": args.max_steps,
        "--swe-timeout": args.swe_timeout,
        "--task-wall-timeout": args.task_wall_timeout,
        "--eval-timeout": args.eval_timeout,
        "--llm-timeout": args.llm_timeout,
        "--total-timeout": args.total_timeout,
    }
    for option, value in positive_values.items():
        if value <= 0:
            parser.error(f"{option} must be > 0")
    if args.checkpoint_interval < 0:
        parser.error("--checkpoint-interval must be >= 0")
    if args.run_id:
        try:
            args.run_id = validate_run_id(args.run_id)
        except ValueError as exc:
            parser.error(str(exc))
    configure_run_paths(args)

    try:
        summary = run_remote(args)
    except KeyboardInterrupt:
        return 130
    write_local_report(summary, args.json_output, args.markdown_output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"done", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
