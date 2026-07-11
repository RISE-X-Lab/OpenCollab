"""Linux subreaper wrapper that proves all command descendants exited."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from collections.abc import Sequence
from typing import Any

TECHNICAL_FAILURE = 125
_PR_SET_CHILD_SUBREAPER = 36
_POLL_SECONDS = 0.01


class SupervisorError(RuntimeError):
    pass


def enable_subreaper() -> None:
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        raise SupervisorError("Linux /proc is required for descendant supervision")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise SupervisorError(f"PR_SET_CHILD_SUBREAPER failed: {os.strerror(error_number)}")


def _proc_parent_and_state(pid: int) -> tuple[int, str] | None:
    try:
        raw = open(f"/proc/{pid}/stat", encoding="ascii").read()  # noqa: SIM115
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupervisorError(f"cannot inspect /proc/{pid}/stat: {exc}") from exc
    close = raw.rfind(")")
    fields = raw[close + 2 :].split() if close >= 0 else []
    if len(fields) < 3:
        raise SupervisorError(f"malformed /proc/{pid}/stat")
    try:
        return int(fields[1]), fields[0]
    except ValueError as exc:
        raise SupervisorError(f"invalid /proc/{pid}/stat") from exc


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
        children = {pid for pid, parent in relations.items() if parent in frontier and pid not in owned}
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


def _reap_adopted(main_child: int | None) -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if main_child is not None and pid == main_child:
            raise SupervisorError("main child was reaped outside its owner")


def terminate_descendants(root_pid: int) -> None:
    for sig, timeout in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + timeout
        empty_scans = 0
        while time.monotonic() < deadline:
            owned = descendants(root_pid)
            if not owned:
                empty_scans += 1
                if empty_scans >= 2:
                    _reap_adopted(None)
                    return
            else:
                empty_scans = 0
                _signal(owned, sig)
            _reap_adopted(None)
            time.sleep(_POLL_SECONDS)
    remaining = descendants(root_pid)
    if remaining:
        raise SupervisorError(f"descendants remained after SIGKILL: {sorted(remaining)[:20]}")


def run(command: Sequence[str]) -> int:
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
    while status is None:
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
    if interrupted:
        return 128 + interrupted
    return os.waitstatus_to_exitcode(status)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    try:
        return run(args)
    except SupervisorError as exc:
        print(f"process supervisor technical failure: {exc}", file=sys.stderr)
        return TECHNICAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
