"""Linux subreaper wrapper that proves all OpenHands descendants exited."""

from __future__ import annotations

import ctypes
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TECHNICAL_FAILURE = 125
_PR_SET_CHILD_SUBREAPER = 36
_POLL_SECONDS = 0.01
_GUARD_MARKER_RE = re.compile(r"[0-9a-f]{32}\.pid(?:\.(?:cancel|lock))?")


class SupervisorError(RuntimeError):
    """Raised when descendant ownership or cleanup cannot be proven."""


def _decode_wait_status(status: int) -> int:
    decoder = getattr(os, "waitstatus_to_exitcode", None)
    if callable(decoder):
        return decoder(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    raise SupervisorError("child returned an unsupported wait status")


def enable_subreaper() -> None:
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        raise SupervisorError("Linux /proc is required for descendant supervision")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise SupervisorError(
            f"PR_SET_CHILD_SUBREAPER failed: {os.strerror(error_number)}"
        )


def _proc_identity(pid: int) -> tuple[int, str, str] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupervisorError(f"cannot inspect /proc/{pid}/stat: {exc}") from exc
    close = raw.rfind(")")
    fields = raw[close + 2 :].split() if close >= 0 else []
    if len(fields) < 20:
        raise SupervisorError(f"malformed /proc/{pid}/stat")
    try:
        return int(fields[1]), fields[0], fields[19]
    except ValueError as exc:
        raise SupervisorError(f"invalid /proc/{pid}/stat") from exc


def _proc_parent_and_state(pid: int) -> tuple[int, str] | None:
    identity = _proc_identity(pid)
    if identity is None:
        return None
    return identity[0], identity[1]


def descendants(root_pid: int) -> set[int]:
    relations: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError as exc:
        raise SupervisorError(f"cannot enumerate /proc: {exc}") from exc
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        value = _proc_parent_and_state(pid)
        if value is not None and value[1] != "Z":
            relations[pid] = value[0]
    owned: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, parent in relations.items()
            if parent in frontier and pid not in owned
        }
        owned.update(children)
        frontier = children
    return owned


def _signal(pids: set[int], sig: signal.Signals) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise SupervisorError(f"cannot signal descendant {pid}: {exc}") from exc


def _reap_adopted() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def terminate_descendants(root_pid: int) -> None:
    for sig, timeout in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + timeout
        empty_scans = 0
        while time.monotonic() < deadline:
            owned = descendants(root_pid)
            if not owned:
                empty_scans += 1
                if empty_scans >= 2:
                    _reap_adopted()
                    return
            else:
                empty_scans = 0
                _signal(owned, sig)
            _reap_adopted()
            time.sleep(_POLL_SECONDS)
    _require_stable_descendant_empty(root_pid)


def _require_stable_descendant_empty(root_pid: int) -> None:
    deadline = time.monotonic() + 0.2
    empty_scans = 0
    last_owned: set[int] = set()
    while time.monotonic() < deadline:
        last_owned = descendants(root_pid)
        if not last_owned:
            empty_scans += 1
            if empty_scans >= 2:
                _reap_adopted()
                return
        else:
            empty_scans = 0
            _signal(last_owned, signal.SIGKILL)
        _reap_adopted()
        time.sleep(_POLL_SECONDS)
    if last_owned:
        raise SupervisorError(
            f"descendants remained after SIGKILL: {sorted(last_owned)[:20]}"
        )
    raise SupervisorError("descendant emptiness was not observed twice")


def _preserved_container_pids() -> set[int]:
    preserved = {1, os.getpid()}
    current = os.getpid()
    while current > 1:
        identity = _proc_identity(current)
        if identity is None:
            raise SupervisorError(f"container quiescer ancestor {current} disappeared")
        parent = identity[0]
        if parent <= 0 or parent in preserved:
            break
        preserved.add(parent)
        current = parent
    return preserved


def _live_container_processes(preserved: set[int]) -> dict[int, str]:
    try:
        entries = os.listdir("/proc")
    except OSError as exc:
        raise SupervisorError(f"cannot enumerate /proc: {exc}") from exc
    live: dict[int, str] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in preserved:
            continue
        identity = _proc_identity(pid)
        if identity is not None and identity[1] != "Z":
            live[pid] = identity[2]
    return live


def _signal_container_processes(
    identities: dict[int, str],
    sig: signal.Signals,
) -> None:
    for pid, expected_start in identities.items():
        current = _proc_identity(pid)
        if current is None or current[1] == "Z" or current[2] != expected_start:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise SupervisorError(f"cannot signal container process {pid}: {exc}") from exc


def quiesce_container() -> None:
    """Stop every live process except PID 1 and this trusted helper's ancestors."""
    enable_subreaper()
    preserved = _preserved_container_pids()
    for sig, timeout in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + timeout
        empty_scans = 0
        while time.monotonic() < deadline:
            live = _live_container_processes(preserved)
            if not live:
                empty_scans += 1
                if empty_scans >= 2:
                    _reap_adopted()
                    return
            else:
                empty_scans = 0
                _signal_container_processes(live, sig)
            _reap_adopted()
            time.sleep(_POLL_SECONDS)
    _require_stable_container_empty(preserved)


def _require_stable_container_empty(preserved: set[int]) -> None:
    deadline = time.monotonic() + 0.2
    empty_scans = 0
    last_live: dict[int, str] = {}
    while time.monotonic() < deadline:
        last_live = _live_container_processes(preserved)
        if not last_live:
            empty_scans += 1
            if empty_scans >= 2:
                _reap_adopted()
                return
        else:
            empty_scans = 0
            _signal_container_processes(last_live, signal.SIGKILL)
        _reap_adopted()
        time.sleep(_POLL_SECONDS)
    if last_live:
        raise SupervisorError(
            "container processes remained after SIGKILL: "
            f"{sorted(last_live)[:20]}"
        )
    raise SupervisorError("container process emptiness was not observed twice")


def prepare_guard_root(path: Path) -> None:
    """Create an empty private marker directory without following entries."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        root_stat = path.lstat()
    except OSError as exc:
        raise SupervisorError(f"cannot inspect guard root {path}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or path.is_symlink():
        raise SupervisorError(f"guard root is not a real directory: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        root_fd = os.open(path, flags)
    except OSError as exc:
        raise SupervisorError(f"cannot safely open guard root {path}: {exc}") from exc
    try:
        for name in os.listdir(root_fd):
            try:
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError as exc:
                raise SupervisorError(f"cannot inspect guard marker {name}: {exc}") from exc
            if _GUARD_MARKER_RE.fullmatch(name) is None or not stat.S_ISREG(current.st_mode):
                raise SupervisorError(f"unexpected guard-root entry: {name}")
            try:
                os.unlink(name, dir_fd=root_fd)
            except OSError as exc:
                raise SupervisorError(f"cannot remove guard marker {name}: {exc}") from exc
        os.fchmod(root_fd, 0o700)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def run(command: Sequence[str], *, timeout_seconds: float | None = None) -> int:
    if not command:
        raise SupervisorError("supervised command is empty")
    enable_subreaper()
    interrupted = 0

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = interrupted or signum

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    child = os.fork()
    if child == 0:
        try:
            os.execvp(command[0], list(command))
        except BaseException as exc:
            print(f"supervised command exec failed: {exc}", file=sys.stderr)
            os._exit(TECHNICAL_FAILURE)

    status: int | None = None
    interrupt_cleanup_started = False
    timed_out = False
    deadline = (
        time.monotonic() + timeout_seconds
        if timeout_seconds is not None
        else None
    )
    while status is None:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            interrupted = interrupted or signal.SIGTERM
        if interrupted and not interrupt_cleanup_started:
            _signal(descendants(os.getpid()), signal.SIGTERM)
            time.sleep(0.1)
            _signal(descendants(os.getpid()), signal.SIGKILL)
            interrupt_cleanup_started = True
        waited, wait_status = os.waitpid(child, os.WNOHANG)
        if waited == child:
            status = wait_status
            break
        time.sleep(_POLL_SECONDS)

    terminate_descendants(os.getpid())
    if timed_out:
        return 124
    if interrupted:
        return 128 + interrupted
    return _decode_wait_status(status)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--prepare-guard-root"]:
        if len(args) != 2:
            print("--prepare-guard-root requires one path", file=sys.stderr)
            return TECHNICAL_FAILURE
        try:
            prepare_guard_root(Path(args[1]))
            return 0
        except Exception as exc:
            print(f"guard-root preparation failure: {exc}", file=sys.stderr)
            return TECHNICAL_FAILURE
    if args == ["--quiesce-container"]:
        try:
            quiesce_container()
            return 0
        except Exception as exc:
            print(f"container quiescence failure: {exc}", file=sys.stderr)
            return TECHNICAL_FAILURE
    timeout_seconds: float | None = None
    if args[:1] == ["--timeout-seconds"]:
        if len(args) < 2:
            print("missing --timeout-seconds value", file=sys.stderr)
            return TECHNICAL_FAILURE
        try:
            timeout_seconds = float(args[1])
        except ValueError:
            print("invalid --timeout-seconds value", file=sys.stderr)
            return TECHNICAL_FAILURE
        if timeout_seconds <= 0:
            print("--timeout-seconds must be positive", file=sys.stderr)
            return TECHNICAL_FAILURE
        args = args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    try:
        return run(args, timeout_seconds=timeout_seconds)
    except Exception as exc:
        try:
            terminate_descendants(os.getpid())
        except Exception as cleanup_exc:
            print(
                f"process supervisor cleanup failure: {cleanup_exc}",
                file=sys.stderr,
            )
        print(f"process supervisor technical failure: {exc}", file=sys.stderr)
        return TECHNICAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
