"""Reusable bounded process-group lifecycle helpers for SWE-bench scripts."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def positive_timeout_seconds(value: object, *, name: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return timeout


def process_group_kwargs() -> dict[str, Any]:
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


def posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_start_identity(pid: int) -> str:
    """Return a reusable-process-safe identity for one live process."""
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


def signal_posix_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def wait_leader(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except (subprocess.TimeoutExpired, ChildProcessError):
        return False


def terminate_process_tree(
    process: subprocess.Popen,
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    if os.name == "posix":
        pgid = process.pid
        signal_posix_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + term_timeout
        while posix_group_exists(pgid) and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_leader(process, min(0.05, max(0.0, remaining)))
            if posix_group_exists(pgid):
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if posix_group_exists(pgid):
            signal_posix_group(pgid, signal.SIGKILL)
        kill_deadline = time.monotonic() + kill_timeout
        leader_reaped = False
        while time.monotonic() < kill_deadline:
            remaining = kill_deadline - time.monotonic()
            leader_reaped = wait_leader(process, min(0.05, remaining)) or leader_reaped
            if leader_reaped and not posix_group_exists(pgid):
                return True
            time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
        return leader_reaped and not posix_group_exists(pgid)

    if os.name == "nt":
        try:
            killed = (
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=kill_timeout,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            killed = False
        if not killed:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        return wait_leader(process, kill_timeout)

    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        pass
    if wait_leader(process, term_timeout):
        return True
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    return wait_leader(process, kill_timeout)


def ensure_process_tree_quiesced_after_wait(
    process: subprocess.Popen,
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    if os.name == "posix" and not posix_group_exists(process.pid):
        return True
    if os.name not in {"posix", "nt"}:
        return wait_leader(process, 0.0)
    return terminate_process_tree(
        process,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )
