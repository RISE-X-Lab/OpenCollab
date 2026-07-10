#!/usr/bin/env python3
"""Thin SWE-bench evaluation status driver.

The script defaults to read-only status generation. Starting evaluation is a
separate action and requires an explicit command template, keeping classification
logic testable and side-effect free.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_decision import task_status_row  # noqa: E402
from opencollab.harness.swe_eval_discovery import build_snapshots  # noqa: E402
from opencollab.harness.swe_eval_records import read_bounded_json  # noqa: E402


CLAIM_CONSTRUCTION_GRACE_SECONDS = 30.0
CLAIM_HEARTBEAT_SECONDS = 5.0
CLAIM_LEASE_SECONDS = 30.0
CLAIM_LEGACY_MAX_AGE_SECONDS = 300.0
MAX_CLAIM_BYTES = 1024 * 1024
MAX_SIDE_NAME_BYTES = 240
SAFE_FILE_OPEN_RETRIES = 8
MAX_REPORT_SCAN_FILES = 10_000
MAX_REPORT_SCAN_ENTRIES = 50_000
MAX_REPORT_SCAN_BYTES = 256 * 1024 * 1024
MAX_REPORT_DOCUMENT_BYTES = 16 * 1024 * 1024
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0

_EVAL_WRAPPER = r"""
import json
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

HEARTBEAT_SECONDS = 5.0
LEASE_SECONDS = 30.0

def write_json(path_text, payload):
    path = Path(os.path.abspath(path_text))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(path.anchor or os.sep, flags)
    try:
        for component in path.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe auto-eval wrapper parent")
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError("auto-eval wrapper parent is not a real directory")
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError("auto-eval wrapper destination is not regular")
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        temp_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            view = memoryview(raw)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("short auto-eval wrapper write")
                view = view[written:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if before is None:
            if current is not None:
                raise OSError("auto-eval wrapper destination appeared")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError("auto-eval wrapper destination changed")
        os.replace(
            temporary,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary = ""
        os.fsync(parent_fd)
    finally:
        try:
            if "temporary" in locals() and temporary:
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
        finally:
            try:
                os.close(parent_fd)
            except OSError:
                pass

def process_group_exists(pgid):
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True

def wait_for_group_exit(pgid, timeout):
    deadline = time.monotonic() + timeout
    while process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True

def terminate_group(pgid):
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if wait_for_group_exit(pgid, 0.5):
        return True
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return wait_for_group_exit(pgid, 2.0)

def terminate_child(child):
    pgid = child.pid
    terminate_group(pgid)
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=1.0)
    return wait_for_group_exit(pgid, 0.5)

def process_start_identity(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
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

claim = json.loads(sys.argv[3])
attempt = json.loads(sys.argv[4])
command = json.loads(sys.argv[5])
pid = os.getpid()

class TerminationRequested(BaseException):
    def __init__(self, signum):
        self.signum = signum

def request_termination(signum, _frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise TerminationRequested(signum)

signal.signal(signal.SIGTERM, request_termination)
signal.signal(signal.SIGINT, request_termination)
child = None
child_pid = 0
exit_code = 1
try:
    child = subprocess.Popen(command, start_new_session=True)
    child_pid = child.pid
    heartbeat_at_ns = time.time_ns()
    identity = {
        "pid": pid,
        "owner_start_identity": process_start_identity(pid),
        "status": "started",
        "evaluator_pid": child_pid,
        "evaluator_pgid": child_pid,
        "evaluator_start_identity": process_start_identity(child_pid),
        "heartbeat_at_ns": heartbeat_at_ns,
        "lease_expires_at_ns": heartbeat_at_ns + int(LEASE_SECONDS * 1_000_000_000),
    }
    claim.update(identity)
    attempt.update(identity)
    write_json(sys.argv[1], claim)
    write_json(sys.argv[2], attempt)
    while True:
        try:
            returncode = child.wait(timeout=HEARTBEAT_SECONDS)
            break
        except subprocess.TimeoutExpired:
            heartbeat_at_ns = time.time_ns()
            heartbeat = {
                "heartbeat_at_ns": heartbeat_at_ns,
                "lease_expires_at_ns": heartbeat_at_ns
                + int(LEASE_SECONDS * 1_000_000_000),
            }
            claim.update(heartbeat)
            attempt.update(heartbeat)
            write_json(sys.argv[1], claim)
            write_json(sys.argv[2], attempt)
    residual_group = process_group_exists(child_pid)
    cleanup_quiesced = not residual_group or terminate_group(child_pid)
    technical_failure = returncode != 0 or residual_group or not cleanup_quiesced
    final = {
        "pid": 0,
        "status": "technical_eval_failed" if technical_failure else "completed",
        "evaluator_returncode": returncode,
        "cleanup_quiesced": cleanup_quiesced,
    }
    if cleanup_quiesced:
        final.update({"evaluator_pid": 0, "evaluator_pgid": 0})
    claim.update(final)
    attempt.update(final)
    write_json(sys.argv[1], claim)
    write_json(sys.argv[2], attempt)
    exit_code = 197 if residual_group or not cleanup_quiesced else returncode
except TerminationRequested as exc:
    cleanup_quiesced = child is None or terminate_child(child)
    final = {
        "pid": 0,
        "status": "technical_eval_failed",
        "termination_signal": exc.signum,
        "cleanup_quiesced": cleanup_quiesced,
    }
    if cleanup_quiesced:
        final.update({"evaluator_pid": 0, "evaluator_pgid": 0})
    claim.update(final)
    attempt.update(final)
    for path, payload in ((sys.argv[1], claim), (sys.argv[2], attempt)):
        try:
            write_json(path, payload)
        except BaseException:
            pass
    exit_code = 128 + exc.signum
except BaseException:
    cleanup_quiesced = child is None or terminate_child(child)
    final = {
        "pid": 0,
        "status": "technical_eval_failed",
        "wrapper_failure": True,
        "cleanup_quiesced": cleanup_quiesced,
    }
    if cleanup_quiesced:
        final.update({"evaluator_pid": 0, "evaluator_pgid": 0})
    claim.update(final)
    attempt.update(final)
    for path, payload in ((sys.argv[1], claim), (sys.argv[2], attempt)):
        try:
            write_json(path, payload)
        except BaseException:
            pass
    raise
finally:
    if child_pid and process_group_exists(child_pid):
        terminate_child(child)
raise SystemExit(exit_code)
"""


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_secure_parent(path: Path, *, create: bool) -> tuple[Path, int, str]:
    absolute = Path(os.path.abspath(path))
    if not absolute.name or absolute.name in {".", ".."}:
        raise OSError(f"invalid auto-eval file path: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError(f"unsafe auto-eval parent component: {path}")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError(f"auto-eval parent must be a real directory: {path}")
            os.close(parent_fd)
            parent_fd = next_fd
        return absolute, parent_fd, absolute.name
    except BaseException:
        os.close(parent_fd)
        raise


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing auto-eval state")
        view = view[written:]


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


def _write_bytes_atomic_at(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    label: object,
) -> None:
    before = _stat_at(parent_fd, name)
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"refusing non-regular auto-eval destination: {label}")
    temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    temp_created = False
    operation_error: BaseException | None = None
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temp_created = True
        try:
            _write_all(temp_fd, payload)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        current = _stat_at(parent_fd, name)
        if before is None:
            if current is not None:
                raise OSError(f"auto-eval destination appeared during write: {label}")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError(f"auto-eval destination changed during write: {label}")
        os.replace(
            temp_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_created = False
        os.fsync(parent_fd)
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except BaseException as cleanup_error:
                if operation_error is None:
                    raise
                add_note = getattr(operation_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "temporary-file unlink failed during cleanup: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            else:
                try:
                    os.fsync(parent_fd)
                except BaseException as cleanup_error:
                    if operation_error is None:
                        raise
                    add_note = getattr(operation_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "temporary-file directory fsync failed during cleanup: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    try:
        _write_bytes_atomic_at(parent_fd, name, payload, label=path)
    finally:
        os.close(parent_fd)


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    _write_bytes_atomic(path, payload)


def _write_markdown(path: Path, summary: dict) -> None:
    lines = [
        "# SWE Auto Eval Status",
        "",
        f"- run_dir: `{summary['run_dir']}`",
        f"- side_name: `{summary['side_name']}`",
        f"- tasks: `{summary['totals']['tasks']}`",
        f"- ready_for_eval: `{summary['totals']['ready_for_eval']}`",
        f"- eval_done: `{summary['totals']['eval_done']}`",
        f"- technical_eval_failed: `{summary['totals']['technical_eval_failed']}`",
        "",
        "| task | state | patch | wf | eval | reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["tasks"]:
        eval_summary = row["eval"]
        eval_label = "none"
        if eval_summary["active_count"]:
            eval_label = "active"
        elif eval_summary["done_count"]:
            eval_label = f"done r={eval_summary['resolved_count']} u={eval_summary['unresolved_count']}"
        elif eval_summary["failed_count"]:
            eval_label = "technical_failed"
        lines.append(
            "| {task} | {state} | {patch} | {wf} | {eval} | {reason} |".format(
                task=row["task"],
                state=row["state"],
                patch=row["patch_len"],
                wf=row["workflow_status"],
                eval=eval_label,
                reason=row["reason"],
            )
        )
    _write_bytes_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))


def build_summary(args: argparse.Namespace) -> dict:
    active_generation = set(args.active_generation_task or [])
    active_eval = set(args.active_eval_task or [])
    snapshots = build_snapshots(
        args.run_dir,
        tasks=args.task or None,
        side_name=args.side_name,
        active_generation_tasks=active_generation,
        active_eval_tasks=active_eval,
    )
    rows = [task_status_row(snapshot, allow_advisory_gap=args.eval_advisory_gap) for snapshot in snapshots]
    totals = {
        "tasks": len(rows),
        "ready_for_eval": sum(1 for row in rows if row["ready_for_eval"]),
        "eval_done": sum(1 for row in rows if row["state"] == "eval_done"),
        "technical_eval_failed": sum(1 for row in rows if row["state"] == "technical_eval_failed"),
        "empty_patch_invalid": sum(1 for row in rows if row["state"] == "empty_patch_invalid"),
    }
    return {
        "schema": "opencollab.swe_auto_eval_status.v1",
        "run_dir": str(args.run_dir),
        "side_name": args.side_name,
        "start_eval": bool(args.start_eval),
        "totals": totals,
        "tasks": rows,
    }


def _format_eval_command(template: str, row: dict) -> list[str]:
    formatted = template.format(
        task=shlex.quote(row["task"]),
        patch_sha=shlex.quote(row["patch_sha256"]),
        record_id=shlex.quote(row.get("record_id") or ""),
    )
    return shlex.split(formatted)


def _command_is_launchable(command: list[str], cwd: Path) -> bool:
    if not command:
        return False
    executable = command[0]
    if "/" in executable:
        path = Path(executable)
        if not path.is_absolute():
            path = cwd / path
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def _identity_file_stem(task: str) -> str:
    import hashlib

    return hashlib.sha256(task.encode("utf-8", errors="surrogatepass")).hexdigest()


def _validate_side_name(value: object) -> str:
    name = str(value)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("side_name must be one non-dot path component")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name):
        raise ValueError("side_name must not contain control, format, or surrogate characters")
    if len(name.encode("utf-8")) > MAX_SIDE_NAME_BYTES:
        raise ValueError(f"side_name exceeds {MAX_SIDE_NAME_BYTES} UTF-8 bytes")
    return name


def _validate_side_directory(run_dir: Path, side_name: object) -> Path:
    name = _validate_side_name(side_name)
    side_dir = run_dir / name
    try:
        info = side_dir.lstat()
    except FileNotFoundError:
        return side_dir
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"auto-eval side path must be a real directory: {side_dir}")
    return side_dir


def _claim_path(args: argparse.Namespace, task: str) -> Path:
    side_dir = _validate_side_directory(args.run_dir, args.side_name)
    return side_dir / ".opencollab" / "claims" / f"{_identity_file_stem(task)}.json"


def _attempt_path(args: argparse.Namespace, row: dict) -> Path:
    identity = f"{row['task']}\0{row.get('record_id') or ''}\0{row['patch_sha256']}"
    side_name = _validate_side_name(args.side_name)
    return (
        args.run_dir
        / side_name
        / ".opencollab"
        / "attempts"
        / f"{_identity_file_stem(identity)}.json"
    )


def _attempt_log_path(args: argparse.Namespace, row: dict) -> Path:
    side_name = _validate_side_name(args.side_name)
    return (
        args.run_dir
        / side_name
        / ".opencollab"
        / "logs"
        / f"{_attempt_path(args, row).stem}.log"
    )


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


def _process_start_identity(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
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


def _process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _claim_lease_is_fresh(claim: dict, file_mtime_ns: int) -> bool:
    now_ns = time.time_ns()
    try:
        expires_at_ns = int(claim.get("lease_expires_at_ns") or 0)
    except (TypeError, ValueError):
        expires_at_ns = 0
    if expires_at_ns > 0:
        return now_ns <= expires_at_ns <= now_ns + int(
            CLAIM_LEASE_SECONDS * 2 * 1_000_000_000
        )
    age = (time.time_ns() - file_mtime_ns) / 1_000_000_000
    return 0 <= age < CLAIM_LEGACY_MAX_AGE_SECONDS


def _claim_owner_is_active(claim: dict, file_mtime_ns: int) -> bool:
    try:
        pid = int(claim.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not _pid_is_active(pid):
        return False
    expected = str(claim.get("owner_start_identity") or "")
    current = _process_start_identity(pid)
    if expected and current and expected != current:
        return False
    return _claim_lease_is_fresh(claim, file_mtime_ns)


def _claim_residual_group_is_live(claim: dict, file_mtime_ns: int) -> bool:
    try:
        pgid = int(claim.get("evaluator_pgid") or 0)
    except (TypeError, ValueError):
        return False
    if pgid <= 1 or not _process_group_exists(pgid):
        return False
    expected = str(claim.get("evaluator_start_identity") or "")
    current = _process_start_identity(pgid)
    if expected and current and expected != current:
        return False
    return _claim_lease_is_fresh(claim, file_mtime_ns)


def _read_json(path: Path, *, max_bytes: int | None = None) -> dict:
    document = read_bounded_json(path, max_bytes=max_bytes)
    if document is None:
        return {}
    value, _opened_stat = document
    return value if isinstance(value, dict) else {}


def _claim_is_recent(path: Path) -> bool:
    try:
        opened = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CLAIM_BYTES:
        return False
    age = time.time() - opened.st_mtime
    return 0 <= age < CLAIM_CONSTRUCTION_GRACE_SECONDS


def _read_json_at(parent_fd: int, name: str, *, label: object) -> dict:
    before = _stat_at(parent_fd, name)
    if before is None:
        return {}
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"claim must be a bounded regular file: {label}")
    if before.st_size > MAX_CLAIM_BYTES:
        return {}
    fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, MAX_CLAIM_BYTES + 1)
        current = _stat_at(parent_fd, name)
    finally:
        os.close(fd)
    if (
        current is None
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or len(raw) > MAX_CLAIM_BYTES
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        or opened.st_size != before.st_size
        or current.st_size != before.st_size
        or opened.st_mtime_ns != before.st_mtime_ns
        or current.st_mtime_ns != before.st_mtime_ns
        or opened.st_ctime_ns != before.st_ctime_ns
        or current.st_ctime_ns != before.st_ctime_ns
    ):
        raise OSError(f"claim changed while reading: {label}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _open_regular_at(
    parent_fd: int,
    name: str,
    flags: int,
    mode: int,
    *,
    label: object,
) -> tuple[int, bool]:
    safe_flags = (
        flags
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        before = _stat_at(parent_fd, name)
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"refusing non-regular auto-eval file: {label}")
        try:
            if before is None:
                fd = os.open(
                    name,
                    safe_flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            else:
                fd = os.open(name, safe_flags, dir_fd=parent_fd)
                created = False
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = _stat_at(parent_fd, name)
            if current is None:
                continue
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise OSError(f"refusing non-regular auto-eval file: {label}")
            opened_identity = (opened.st_dev, opened.st_ino)
            if (current.st_dev, current.st_ino) != opened_identity:
                continue
            if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                continue
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
    raise OSError(f"auto-eval file did not stabilize while opening: {label}")


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    try:
        return _open_regular_at(parent_fd, name, flags, mode, label=path)
    finally:
        os.close(parent_fd)


def _open_append_binary(path: Path):
    fd, _created = _open_regular_file(path, os.O_WRONLY | os.O_APPEND, 0o644)
    return os.fdopen(fd, "ab")


def _unlink_durable(path: Path) -> None:
    _absolute, parent_fd, name = _open_secure_parent(path, create=False)
    try:
        current = _stat_at(parent_fd, name)
        if current is None:
            return
        if not stat.S_ISREG(current.st_mode):
            raise OSError(f"refusing to unlink non-regular auto-eval file: {path}")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _claim_is_bootstrapping(existing: dict) -> bool:
    if existing.get("status") not in {"claiming", "launching"}:
        return False
    try:
        started_at_ns = int(existing.get("started_at_ns") or 0)
    except (TypeError, ValueError):
        return False
    if started_at_ns <= 0:
        return False
    age_seconds = (time.time_ns() - started_at_ns) / 1_000_000_000
    return 0 <= age_seconds < CLAIM_CONSTRUCTION_GRACE_SECONDS


def _acquire_claim(path: Path, payload: dict) -> tuple[bool, dict]:
    """Serialize claim inspection/reclamation and publish complete JSON atomically."""
    _absolute, parent_fd, name = _open_secure_parent(path, create=True)
    lock_name = f"{name}.lock"
    lock_fd, _created = _open_regular_at(
        parent_fd,
        lock_name,
        os.O_RDWR,
        0o600,
        label=path.with_name(lock_name),
    )
    locked = False
    try:
        _acquire_exclusive_lock(lock_fd, label=f"claim lock {path.with_name(lock_name)}")
        locked = True
        current = _stat_at(parent_fd, name)
        if current is not None:
            existing = _read_json_at(parent_fd, name, label=path)
            if _claim_owner_is_active(existing, current.st_mtime_ns):
                return False, existing
            if _claim_residual_group_is_live(existing, current.st_mtime_ns):
                return False, existing
            if _claim_is_bootstrapping(existing):
                return False, existing
            # Interoperate safely with a writer from an older process that may
            # have created the destination before finishing its JSON payload.
            # A fresh malformed file is treated as under construction; a stale
            # one can be reclaimed after the grace period.
            age = time.time() - current.st_mtime
            if (
                not existing
                and stat.S_ISREG(current.st_mode)
                and current.st_size <= MAX_CLAIM_BYTES
                and 0 <= age < CLAIM_CONSTRUCTION_GRACE_SECONDS
            ):
                return False, {"status": "claim_in_progress"}
            if not stat.S_ISREG(current.st_mode):
                raise OSError(f"claim must be a regular file: {path}")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)

        # _write_json first creates a complete sibling temp file and then
        # replaces the destination. Contenders cannot observe an empty or
        # half-written claim, while the advisory lock also closes stale-claim
        # check/delete races between cooperating processes.
        claim_payload = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _write_bytes_atomic_at(
            parent_fd,
            name,
            claim_payload,
            label=path,
        )
        return True, payload
    finally:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(parent_fd)


def _open_real_directory(path: Path) -> int:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"auto-eval report directory cannot be inspected: {path}") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"auto-eval report directory must be real: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"auto-eval report directory cannot be opened: {path}") from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(fd)
        raise ValueError(f"auto-eval report directory changed while opening: {path}")
    return fd


def _iter_report_json_paths(side_dir: Path):
    pending = [side_dir]
    scanned_entries = 0
    while pending:
        directory = pending.pop()
        fd = _open_real_directory(directory)
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    scanned_entries += 1
                    if scanned_entries > MAX_REPORT_SCAN_ENTRIES:
                        raise ValueError(
                            "auto-eval report scan exceeds "
                            f"{MAX_REPORT_SCAN_ENTRIES} directory entries"
                        )
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    path = directory / entry.name
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode) and entry.name.endswith(".json"):
                        yield path
        finally:
            os.close(fd)


def _report_fingerprints(side_dir: Path, task: str) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    try:
        root_info = side_dir.lstat()
    except FileNotFoundError:
        return fingerprints
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"auto-eval report root must be a real directory: {side_dir}")
    scanned_files = 0
    scanned_bytes = 0
    for path in _iter_report_json_paths(side_dir):
        scanned_files += 1
        if scanned_files > MAX_REPORT_SCAN_FILES:
            raise ValueError(
                f"auto-eval report scan exceeds {MAX_REPORT_SCAN_FILES} JSON files"
            )
        try:
            entry_info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(entry_info.st_mode):
            continue
        if entry_info.st_size > MAX_REPORT_DOCUMENT_BYTES:
            raise ValueError(f"auto-eval report exceeds byte limit: {path}")
        scanned_bytes += entry_info.st_size
        if scanned_bytes > MAX_REPORT_SCAN_BYTES:
            raise ValueError(
                f"auto-eval report scan exceeds {MAX_REPORT_SCAN_BYTES} bytes"
            )
        document = read_bounded_json(path, max_bytes=MAX_REPORT_DOCUMENT_BYTES)
        if document is None or not isinstance(document[0], dict):
            continue
        payload, opened = document
        scanned_bytes += max(0, opened.st_size - entry_info.st_size)
        if scanned_bytes > MAX_REPORT_SCAN_BYTES:
            raise ValueError(
                f"auto-eval report scan exceeds {MAX_REPORT_SCAN_BYTES} bytes"
            )
        if payload.get("schema") in {
            "opencollab.swe_eval_attempt.v1",
            "opencollab.swe_eval_claim.v1",
        }:
            continue
        if str(
            payload.get("instance_id")
            or payload.get("task_id")
            or payload.get("task")
            or ""
        ) != task:
            item = payload.get(task)
            if not isinstance(item, dict):
                continue
        try:
            relative = str(path.relative_to(side_dir))
        except (OSError, ValueError):
            continue
        fingerprints[relative] = (
            f"{opened.st_mtime_ns}:{opened.st_ctime_ns}:"
            f"{opened.st_size}:{opened.st_ino}"
        )
    return fingerprints


def _candidate_identity(
    row: dict,
    started_at_ns: int,
    *,
    status: str,
    pid: int,
    prior_reports: dict[str, str] | None = None,
) -> dict:
    identity = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": row["task"],
        "record_id": row.get("record_id") or "",
        "patch_sha256": row["patch_sha256"],
        "started_at_ns": started_at_ns,
        "heartbeat_at_ns": started_at_ns,
        "lease_expires_at_ns": started_at_ns
        + int(CLAIM_LEASE_SECONDS * 1_000_000_000),
        "status": status,
        "pid": pid,
    }
    if pid > 0:
        identity["owner_start_identity"] = _process_start_identity(pid)
    if prior_reports is not None:
        identity["prior_reports"] = prior_reports
    return identity


def _wrapped_eval_command(
    command: list[str],
    claim_path: Path,
    attempt_path: Path,
    started: dict,
) -> list[str]:
    claim = {**started, "schema": "opencollab.swe_eval_claim.v1"}
    return [
        sys.executable,
        "-c",
        _EVAL_WRAPPER,
        str(claim_path),
        str(attempt_path),
        json.dumps(claim, ensure_ascii=False),
        json.dumps(started, ensure_ascii=False),
        json.dumps(command, ensure_ascii=False),
    ]


def maybe_start_eval(args: argparse.Namespace, summary: dict) -> list[dict]:
    if not args.start_eval:
        return []
    if not args.eval_command_template:
        raise SystemExit("--start-eval requires --eval-command-template")
    _validate_side_directory(args.run_dir, args.side_name)
    actions: list[dict] = []
    starts = 0
    for row in summary["tasks"]:
        if starts >= args.max_eval_starts:
            break
        if not row["ready_for_eval"]:
            continue
        command = _format_eval_command(args.eval_command_template, row)
        if args.dry_run:
            actions.append({"task": row["task"], "action": "dry_run", "command": command})
            starts += 1
            continue
        if not _command_is_launchable(command, args.run_dir):
            actions.append(
                {
                    "task": row["task"],
                    "action": "failed_to_start",
                    "error": f"executable not found: {command[0] if command else ''}",
                    "command": command,
                }
            )
            continue
        patch_sha = str(row.get("patch_sha256") or "")
        record_id = str(row.get("record_id") or "")
        if len(patch_sha) != 64 or not record_id:
            actions.append(
                {
                    "task": row["task"],
                    "action": "invalid_candidate_identity",
                    "record_id": record_id,
                    "patch_sha256": patch_sha,
                }
            )
            continue
        started_at_ns = time.time_ns()
        claim_path = _claim_path(args, row["task"])
        claim = {
            **_candidate_identity(row, started_at_ns, status="claiming", pid=os.getpid()),
            "schema": "opencollab.swe_eval_claim.v1",
        }
        acquired, existing = _acquire_claim(claim_path, claim)
        if not acquired:
            actions.append(
                {
                    "task": row["task"],
                    "action": "already_claimed",
                    "claim": existing,
                }
            )
            continue
        attempt_path = _attempt_path(args, row)
        prior_reports = _report_fingerprints(args.run_dir / args.side_name, row["task"])
        _write_json(
            attempt_path,
            _candidate_identity(
                row,
                started_at_ns,
                status="launching",
                pid=os.getpid(),
                prior_reports=prior_reports,
            ),
        )
        log_path = _attempt_log_path(args, row)
        started = _candidate_identity(
            row,
            started_at_ns,
            status="started",
            pid=0,
            prior_reports=prior_reports,
        )
        launch_command = _wrapped_eval_command(command, claim_path, attempt_path, started)
        try:
            with _open_append_binary(log_path) as log_handle:
                proc = subprocess.Popen(
                    launch_command,
                    cwd=args.run_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            _write_json(
                attempt_path,
                _candidate_identity(
                    row,
                    started_at_ns,
                    status="failed_to_start",
                    pid=0,
                    prior_reports=prior_reports,
                ),
            )
            _unlink_durable(claim_path)
            actions.append(
                {"task": row["task"], "action": "failed_to_start", "error": str(exc), "command": command}
            )
            continue
        actions.append(
            {
                "task": row["task"],
                "action": "started",
                "pid": proc.pid,
                "command": command,
                "log": str(log_path),
            }
        )
        starts += 1
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize and optionally start SWE-bench eval tasks.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--side-name", default="official_eval_auto")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--active-generation-task", action="append", default=[])
    parser.add_argument("--active-eval-task", action="append", default=[])
    parser.add_argument("--eval-advisory-gap", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    parser.add_argument("--start-eval", action="store_true")
    parser.add_argument(
        "--eval-command-template",
        default="",
        help=(
            "Command template parsed with shlex; shell redirection and pipes are not interpreted. "
            "Use bash -lc when shell syntax is required."
        ),
    )
    parser.add_argument("--max-eval-starts", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    args.run_dir = args.run_dir.resolve()
    try:
        args.side_name = _validate_side_name(args.side_name)
        _validate_side_directory(args.run_dir, args.side_name)
    except ValueError as exc:
        parser.error(str(exc))
    summary = build_summary(args)
    actions = maybe_start_eval(args, summary)
    if actions:
        summary["actions"] = actions
    if args.json_output:
        _write_json(args.json_output, summary)
    if args.markdown_output:
        _write_markdown(args.markdown_output, summary)
    if not args.json_output and not args.markdown_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if any(
        action.get("action") in {"failed_to_start", "invalid_candidate_identity"}
        for action in actions
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
