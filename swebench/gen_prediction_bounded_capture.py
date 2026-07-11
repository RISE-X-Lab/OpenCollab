"""Bound stdout and stderr while owning the captured process group."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence

OVERFLOW_EXIT_CODE = 86
CLEANUP_FAILURE_EXIT_CODE = 125


def process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def wait_for_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def terminate_owned_group(proc: subprocess.Popen[bytes]) -> bool:
    try:
        leader_reaped = proc.poll() is not None
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        if not leader_reaped:
            try:
                proc.wait(timeout=0.25)
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
        group_gone = wait_for_group_exit(proc.pid, 0.25)
        if leader_reaped and group_gone:
            return True

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        if not leader_reaped:
            try:
                proc.wait(timeout=1.0)
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
        group_gone = wait_for_group_exit(proc.pid, 1.0)
        return leader_reaped and group_gone
    except BaseException:
        return False


def _positive_limit(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def capture(command: Sequence[str], *, stdout_limit: int, stderr_limit: int, label: str) -> int:
    if not command:
        raise ValueError("capture command must not be empty")
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if proc.stdout is None or proc.stderr is None:
        terminate_owned_group(proc)
        raise RuntimeError("capture pipes were not created")
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
    selector.register(proc.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = ""
    while selector.get_map() and not overflow:
        for key, _events in selector.select(timeout=0.1):
            stream_name, limit = key.data
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            target = captured[stream_name]
            remaining = max(0, limit - len(target))
            target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow = stream_name
                break
        if not overflow and proc.poll() is not None and process_group_exists(proc.pid):
            if not terminate_owned_group(proc):
                sys.stderr.buffer.write(captured["stderr"])
                sys.stderr.write("bounded capture process group did not quiesce\n")
                return CLEANUP_FAILURE_EXIT_CODE

    if overflow:
        cleanup_quiesced = terminate_owned_group(proc)
        sys.stderr.buffer.write(captured["stderr"])
        sys.stderr.write(f"{label} {overflow} exceeded its byte limit\n")
        if not cleanup_quiesced:
            sys.stderr.write("bounded capture process group did not quiesce\n")
            return CLEANUP_FAILURE_EXIT_CODE
        return OVERFLOW_EXIT_CODE

    returncode = proc.wait()
    if process_group_exists(proc.pid) and not terminate_owned_group(proc):
        sys.stderr.buffer.write(captured["stderr"])
        sys.stderr.write("bounded capture process group did not quiesce\n")
        return CLEANUP_FAILURE_EXIT_CODE
    sys.stderr.buffer.write(captured["stderr"])
    if returncode != 0:
        return returncode
    sys.stdout.buffer.write(captured["stdout"])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4:
        print(
            "usage: gen_prediction_bounded_capture.py STDOUT_LIMIT STDERR_LIMIT LABEL COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 2
    try:
        stdout_limit = _positive_limit(args[0], "stdout limit")
        stderr_limit = _positive_limit(args[1], "stderr limit")
        return capture(
            args[3:],
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            label=args[2],
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
