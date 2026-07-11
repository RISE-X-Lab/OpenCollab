#!/usr/bin/env python3
"""Own and stop one container-side process session without orphaning descendants."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

UNKNOWN = 125
_MARKER_LIMIT = 4096
_POLL_SECONDS = 0.05
_SESSION_ENUMERATION_TIMEOUT_SECONDS = 3.0


class GuardError(RuntimeError):
    pass


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_parent(path: Path) -> tuple[Path, int]:
    target = _absolute(path)
    flags = _directory_flags()
    fd = os.open(target.anchor or os.sep, flags)
    try:
        for component in target.parent.parts[1:]:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise GuardError(f"marker parent is not a real directory: {target.parent}")
            next_fd = os.open(component, flags, dir_fd=fd)
            opened = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                os.close(next_fd)
                raise GuardError(f"marker parent changed while opening: {target.parent}")
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return target, result
    except OSError as exc:
        raise GuardError(f"cannot safely open marker parent {target.parent}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _parent_matches(path: Path, parent_fd: int) -> bool:
    try:
        _target, verified_fd = _open_parent(path)
    except GuardError:
        return False
    try:
        original = os.fstat(parent_fd)
        verified = os.fstat(verified_fd)
        return (original.st_dev, original.st_ino) == (verified.st_dev, verified.st_ino)
    finally:
        os.close(verified_fd)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _exists_nofollow(path: Path) -> bool:
    target, parent_fd = _open_parent(path)
    try:
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(current.st_mode) or current.st_size > _MARKER_LIMIT:
            raise GuardError(f"marker is not a bounded regular file: {target}")
        if not _parent_matches(target, parent_fd):
            raise GuardError(f"marker parent changed while inspecting: {target.parent}")
        return True
    finally:
        os.close(parent_fd)


def _write_new_regular(path: Path, payload: bytes, *, existing_ok: bool) -> bool:
    target, parent_fd = _open_parent(path)
    fd = -1
    created = False
    try:
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None:
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MARKER_LIMIT:
                raise GuardError(f"marker is not a bounded regular file: {target}")
            if existing_ok:
                return False
            raise FileExistsError(target)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            current = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(current.st_mode) or current.st_size > _MARKER_LIMIT:
                raise GuardError(f"marker is not a bounded regular file: {target}")
            if existing_ok and _parent_matches(target, parent_fd):
                return False
            raise
        created = True
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise GuardError(f"marker is not a regular file: {target}")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise GuardError("marker write made no progress")
            view = view[written:]
        os.fsync(fd)
        after = os.fstat(fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(after):
            raise GuardError(f"marker changed while writing: {target}")
        if not _parent_matches(target, parent_fd):
            raise GuardError(f"marker parent changed while writing: {target.parent}")
        os.fsync(parent_fd)
        return True
    except BaseException:
        if created:
            try:
                current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(fd) if fd >= 0 else None
                if opened is not None and (current.st_dev, current.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    os.unlink(target.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except (FileNotFoundError, OSError):
                pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _read_proc_stat(pid: int) -> tuple[int, str, str] | None:
    path = Path("/proc") / str(pid) / "stat"
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GuardError(f"cannot read {path}: {exc}") from exc
    close = raw.rfind(")")
    if close < 0:
        raise GuardError(f"malformed {path}")
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        raise GuardError(f"short {path}")
    try:
        session_id = int(fields[3])
    except ValueError as exc:
        raise GuardError(f"invalid session id in {path}") from exc
    return session_id, fields[0], fields[19]


def _start_identity(pid: int) -> str:
    if Path("/proc").is_dir():
        value = _read_proc_stat(pid)
        return value[2] if value is not None else ""
    return ""


def _proc_session_members(session_id: int) -> set[int]:
    members: set[int] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise GuardError(f"cannot enumerate /proc: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        value = _read_proc_stat(int(entry.name))
        if value is None:
            continue
        sid, state, _start = value
        if sid == session_id and state != "Z":
            members.add(int(entry.name))
    return members


def _command_session_members(session_id: int) -> set[int]:
    pgrep = shutil.which("pgrep")
    if pgrep:
        try:
            result = subprocess.run(
                [pgrep, "-s", str(session_id)],
                capture_output=True,
                text=True,
                timeout=_SESSION_ENUMERATION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GuardError(f"pgrep session enumeration failed: {exc}") from exc
        if result.returncode == 1:
            return set()
        if result.returncode != 0:
            raise GuardError(
                f"pgrep session enumeration exited {result.returncode}"
            )
        if result.returncode == 0:
            values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not values:
                raise GuardError("pgrep returned success without process ids")
            if any(re.fullmatch(r"[0-9]+", value) is None for value in values):
                raise GuardError("pgrep returned a malformed process id")
            candidates = {int(value) for value in values}
            ps = shutil.which("ps")
            if not ps:
                raise GuardError("ps is required to filter zombie pgrep results")
            try:
                states = subprocess.run(
                    [ps, "-o", "pid=,state=", "-p", ",".join(map(str, sorted(candidates)))],
                    capture_output=True,
                    text=True,
                    timeout=_SESSION_ENUMERATION_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GuardError(f"ps zombie filtering failed: {exc}") from exc
            if states.returncode not in {0, 1}:
                raise GuardError(f"ps zombie filtering exited {states.returncode}")
            live: set[int] = set()
            for line in states.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2 or not fields[0].isdigit():
                    raise GuardError("ps returned malformed zombie-filter data")
                pid = int(fields[0])
                if pid not in candidates:
                    raise GuardError("ps returned an unexpected process id")
                if not fields[1].startswith("Z"):
                    live.add(pid)
            return live

    ps = shutil.which("ps")
    if not ps:
        raise GuardError("neither /proc, pgrep, nor ps is available")
    try:
        result = subprocess.run(
            [ps, "-axo", "pid=,sid=,state="],
            capture_output=True,
            text=True,
            timeout=_SESSION_ENUMERATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"ps session enumeration failed: {exc}") from exc
    if result.returncode != 0:
        raise GuardError(f"ps session enumeration exited {result.returncode}")
    members: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise GuardError("ps returned malformed session data")
        if int(fields[1]) == session_id and not fields[2].startswith("Z"):
            members.add(int(fields[0]))
    return members


def _session_members(session_id: int) -> set[int]:
    if Path("/proc").is_dir():
        return _proc_session_members(session_id)
    return _command_session_members(session_id)


def _validate_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardError("owner marker is not an object")
    if value.get("schema") != "opencollab.container-process.v1":
        raise GuardError("owner marker schema mismatch")
    session_id = value.get("session_id")
    owner_pid = value.get("owner_pid")
    nonce = value.get("nonce")
    start_identity = value.get("start_identity")
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 1:
        raise GuardError("owner marker has invalid session id")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 1:
        raise GuardError("owner marker has invalid owner pid")
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise GuardError("owner marker has invalid nonce")
    if not isinstance(start_identity, str):
        raise GuardError("owner marker has invalid start identity")
    return value


def _read_record(path: Path) -> dict[str, Any]:
    target, parent_fd = _open_parent(path)
    fd = -1
    try:
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise GuardError("owner marker is missing") from exc
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MARKER_LIMIT:
            raise GuardError("owner marker is not a bounded regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(target.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise GuardError("owner marker changed while opening")
        chunks: list[bytes] = []
        remaining = _MARKER_LIMIT + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, _MARKER_LIMIT + 1))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(after) != _identity(opened) or _identity(current) != _identity(after):
            raise GuardError("owner marker changed while reading")
        if not _parent_matches(target, parent_fd):
            raise GuardError("owner marker parent changed while reading")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
    payload = b"".join(chunks)
    if len(payload) > _MARKER_LIMIT:
        raise GuardError("owner marker exceeds its size bound")
    try:
        return _validate_record(json.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("owner marker is malformed") from exc


def _write_record(path: Path, record: dict[str, Any]) -> None:
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii")
    _write_new_regular(path, payload, existing_ok=False)


def _remove_owned_record(path: Path, record: dict[str, Any]) -> None:
    try:
        current = _read_record(path)
    except GuardError:
        if _exists_nofollow(path):
            raise
        return
    if (
        current.get("nonce") == record.get("nonce")
        and current.get("session_id") == record.get("session_id")
    ):
        target, parent_fd = _open_parent(path)
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise GuardError("owner marker changed before removal")
            os.unlink(target.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            if not _parent_matches(target, parent_fd):
                raise GuardError("owner marker parent changed during removal")
        except FileNotFoundError:
            return
        finally:
            os.close(parent_fd)


def _create_cancel_marker(path: Path) -> None:
    _write_new_regular(path, b"cancel\n", existing_ok=True)


def _unlink_path(path: Path) -> None:
    target, parent_fd = _open_parent(path)
    try:
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(current.st_mode) or current.st_size > _MARKER_LIMIT:
            raise GuardError(f"refusing to unlink unsafe marker: {target}")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if not _parent_matches(target, parent_fd):
            raise GuardError(f"marker parent changed during unlink: {target.parent}")
    finally:
        os.close(parent_fd)


def _open_owner_lock(pidfile: Path) -> int:
    lock_path = Path(str(pidfile) + ".lock")
    target, parent_fd = _open_parent(lock_path)
    fd = -1
    try:
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise GuardError("owner lock is not a regular file")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        opened = os.fstat(fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            raise GuardError("owner lock is not a regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise GuardError("owner lock changed while opening")
        if before is not None and (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise GuardError("owner lock changed while opening")
        if not _parent_matches(target, parent_fd):
            raise GuardError("owner lock parent changed while opening")
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            return False
        raise


def _assert_identity(record: dict[str, Any], *, trusted: bool) -> None:
    session_id = int(record["session_id"])
    expected = str(record.get("start_identity") or "")
    if trusted:
        return
    if not expected:
        raise GuardError("stale owner marker lacks a verifiable start identity")
    current = _start_identity(session_id)
    if current and current != expected:
        raise GuardError("session leader identity changed; refusing to signal reused pid")


def _signal_members(session_id: int, members: set[int], sig: signal.Signals) -> None:
    try:
        os.killpg(session_id, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Session enumeration below gives us every member, including processes
        # that moved into a nested process group. Individual signalling plus
        # the post-signal emptiness proof remains authoritative.
        pass
    for pid in members:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise GuardError(f"cannot signal process {pid}: {exc}") from exc


def _terminate_session(record: dict[str, Any], *, trusted: bool) -> None:
    _assert_identity(record, trusted=trusted)
    session_id = int(record["session_id"])
    for sig, duration in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + duration
        empty_scans = 0
        while time.monotonic() < deadline:
            members = _session_members(session_id)
            if not members:
                empty_scans += 1
                if empty_scans >= 2:
                    return
            else:
                empty_scans = 0
                _signal_members(session_id, members, sig)
            time.sleep(_POLL_SECONDS)
    remaining = _session_members(session_id)
    if remaining:
        raise GuardError(
            f"session {session_id} remained after SIGKILL: {sorted(remaining)[:20]}"
        )


def _wait_for_record(path: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: GuardError | None = None
    while time.monotonic() < deadline:
        try:
            return _read_record(path)
        except GuardError as exc:
            last_error = exc
        time.sleep(_POLL_SECONDS)
    raise last_error or GuardError("owner marker did not appear")


def prepare(pidfile: Path, cancelfile: Path) -> int:
    lock_fd = _open_owner_lock(pidfile)
    try:
        if not _try_lock(lock_fd):
            raise GuardError("another process guard owns this marker")
        if _exists_nofollow(pidfile):
            record = _read_record(pidfile)
            _terminate_session(record, trusted=False)
            _remove_owned_record(pidfile, record)
        _unlink_path(cancelfile)
        return 0
    finally:
        os.close(lock_fd)


def stop(pidfile: Path, cancelfile: Path) -> int:
    _create_cancel_marker(cancelfile)
    lock_fd = _open_owner_lock(pidfile)
    try:
        acquired = _try_lock(lock_fd)
        if acquired and not _exists_nofollow(pidfile):
            return 0
        record = (
            _read_record(pidfile)
            if acquired
            else _wait_for_record(pidfile, timeout=2.0)
        )
        _terminate_session(record, trusted=not acquired)
        if acquired:
            _remove_owned_record(pidfile, record)
            return 0

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _try_lock(lock_fd):
                if _exists_nofollow(pidfile):
                    current = _read_record(pidfile)
                    if current.get("nonce") == record.get("nonce"):
                        _remove_owned_record(pidfile, current)
                return 0
            time.sleep(_POLL_SECONDS)
        raise GuardError("guard owner did not release after session termination")
    finally:
        os.close(lock_fd)


def _child_main(
    pidfile: Path,
    cancelfile: Path,
    command: list[str],
    nonce: str,
) -> None:
    os.setsid()
    record = {
        "schema": "opencollab.container-process.v1",
        "session_id": os.getpid(),
        "owner_pid": os.getppid(),
        "start_identity": _start_identity(os.getpid()),
        "nonce": nonce,
    }
    _write_record(pidfile, record)
    if _exists_nofollow(cancelfile):
        raise SystemExit(UNKNOWN)
    os.execvp(command[0], command)


def run(pidfile: Path, cancelfile: Path, command: list[str]) -> int:
    if not command:
        raise GuardError("missing guarded command")
    lock_fd = _open_owner_lock(pidfile)
    if not _try_lock(lock_fd):
        os.close(lock_fd)
        raise GuardError("another process guard owns this marker")
    if _exists_nofollow(pidfile):
        os.close(lock_fd)
        raise GuardError("owner marker already exists; run prepare/stop first")
    if _exists_nofollow(cancelfile):
        os.close(lock_fd)
        return UNKNOWN

    nonce = uuid.uuid4().hex
    child = os.fork()
    if child == 0:
        try:
            os.close(lock_fd)
            _child_main(pidfile, cancelfile, command, nonce)
        except BaseException as exc:
            print(f"process guard child failed: {exc}", file=sys.stderr)
            os._exit(UNKNOWN)

    interrupted = 0

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = interrupted or signum

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    record: dict[str, Any] | None = None
    status: int | None = None
    cleanup_error: GuardError | None = None
    interrupt_signalled = False
    try:
        while status is None:
            if record is None:
                try:
                    candidate = _read_record(pidfile)
                    if candidate.get("nonce") == nonce:
                        record = candidate
                except GuardError:
                    pass
            if interrupted and record is not None and not interrupt_signalled:
                try:
                    members = _session_members(int(record["session_id"]))
                    _signal_members(
                        int(record["session_id"]),
                        members,
                        signal.SIGTERM,
                    )
                    time.sleep(0.1)
                    members = _session_members(int(record["session_id"]))
                    _signal_members(
                        int(record["session_id"]),
                        members,
                        signal.SIGKILL,
                    )
                    interrupt_signalled = True
                except GuardError as exc:
                    cleanup_error = exc
            waited, wait_status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                status = wait_status
                break
            time.sleep(_POLL_SECONDS)

        if record is None:
            try:
                candidate = _read_record(pidfile)
                if candidate.get("nonce") == nonce:
                    record = candidate
            except GuardError:
                record = {
                    "schema": "opencollab.container-process.v1",
                    "session_id": child,
                    "owner_pid": os.getpid(),
                    "start_identity": _start_identity(child),
                    "nonce": nonce,
                }
        try:
            _terminate_session(record, trusted=True)
        except GuardError as exc:
            cleanup_error = exc
        if cleanup_error is None:
            _remove_owned_record(pidfile, record)
    finally:
        os.close(lock_fd)

    if cleanup_error is not None:
        raise cleanup_error
    if interrupted:
        return 128 + interrupted
    assert status is not None
    return os.waitstatus_to_exitcode(status)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in {"prepare", "run", "stop"}:
        raise GuardError(
            "usage: container_process_guard.sh prepare|stop <pidfile> <cancelfile> "
            "| run <pidfile> <cancelfile> <command> [args...]"
        )
    mode = argv[0]
    pidfile = Path(argv[1])
    cancelfile = Path(argv[2])
    if mode == "prepare":
        if len(argv) != 3:
            raise GuardError("prepare takes exactly two paths")
        return prepare(pidfile, cancelfile)
    if mode == "stop":
        if len(argv) != 3:
            raise GuardError("stop takes exactly two paths")
        return stop(pidfile, cancelfile)
    return run(pidfile, cancelfile, argv[3:])


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except GuardError as exc:
        print(f"process guard error: {exc}", file=sys.stderr)
        raise SystemExit(UNKNOWN) from exc
    except Exception as exc:
        print(f"process guard technical failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(UNKNOWN) from exc
