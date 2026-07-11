#!/usr/bin/env python3
"""Own one auto-evaluation child and durably publish its claim heartbeat."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from opencollab.adapters.safe_files import write_regular_bytes_atomic  # noqa: E402
from swe_auto_eval_constants import (  # noqa: E402
    CLAIM_HEARTBEAT_SECONDS,
    CLAIM_LEASE_SECONDS,
)
from swebench_process import (  # noqa: E402
    posix_group_exists,
    process_start_identity,
    terminate_process_tree,
)

HEARTBEAT_SECONDS = CLAIM_HEARTBEAT_SECONDS
LEASE_SECONDS = CLAIM_LEASE_SECONDS
TERM_SECONDS = 0.5
KILL_SECONDS = 2.0
RESIDUAL_GROUP_EXIT = 197


class TerminationRequested(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def write_json(path_text: str, payload: dict[str, Any]) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_regular_bytes_atomic(Path(path_text), raw)


def request_termination(signum: int, _frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise TerminationRequested(signum)


def terminate_child(child: subprocess.Popen[Any]) -> bool:
    return terminate_process_tree(
        child,
        term_timeout=TERM_SECONDS,
        kill_timeout=KILL_SECONDS,
    )


def _publish_both(
    claim_path: str,
    attempt_path: str,
    claim: dict[str, Any],
    attempt: dict[str, Any],
) -> None:
    write_json(claim_path, claim)
    write_json(attempt_path, attempt)


def run(
    claim_path: str,
    attempt_path: str,
    claim: dict[str, Any],
    attempt: dict[str, Any],
    command: list[str],
) -> int:
    pid = os.getpid()
    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGINT, request_termination)
    child: subprocess.Popen[Any] | None = None
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
        _publish_both(claim_path, attempt_path, claim, attempt)
        while True:
            try:
                returncode = child.wait(timeout=HEARTBEAT_SECONDS)
                break
            except subprocess.TimeoutExpired:
                heartbeat_at_ns = time.time_ns()
                heartbeat = {
                    "heartbeat_at_ns": heartbeat_at_ns,
                    "lease_expires_at_ns": heartbeat_at_ns + int(LEASE_SECONDS * 1_000_000_000),
                }
                claim.update(heartbeat)
                attempt.update(heartbeat)
                _publish_both(claim_path, attempt_path, claim, attempt)
        residual_group = posix_group_exists(child_pid)
        cleanup_quiesced = not residual_group or terminate_child(child)
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
        _publish_both(claim_path, attempt_path, claim, attempt)
        exit_code = RESIDUAL_GROUP_EXIT if residual_group or not cleanup_quiesced else returncode
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
        for path, payload in ((claim_path, claim), (attempt_path, attempt)):
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
        for path, payload in ((claim_path, claim), (attempt_path, attempt)):
            try:
                write_json(path, payload)
            except BaseException:
                pass
        raise
    finally:
        if child is not None and child_pid and posix_group_exists(child_pid):
            terminate_child(child)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 5:
        raise SystemExit("expected claim path, attempt path, claim, attempt, and command")
    claim_path, attempt_path, raw_claim, raw_attempt, raw_command = args
    claim = json.loads(raw_claim)
    attempt = json.loads(raw_attempt)
    command = json.loads(raw_command)
    if not isinstance(claim, dict) or not isinstance(attempt, dict):
        raise ValueError("claim and attempt must be JSON objects")
    if not isinstance(command, list) or not command or not all(isinstance(value, str) and value for value in command):
        raise ValueError("command must be a non-empty JSON string list")
    return run(claim_path, attempt_path, claim, attempt, command)


if __name__ == "__main__":
    raise SystemExit(main())
