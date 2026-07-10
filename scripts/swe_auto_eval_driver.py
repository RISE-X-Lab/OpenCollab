#!/usr/bin/env python3
"""Thin SWE-bench evaluation status driver.

The script defaults to read-only status generation. Starting evaluation is a
separate action and requires an explicit command template, keeping classification
logic testable and side-effect free.
"""

# ruff: noqa: E402, F401

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
import types
import unicodedata
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.harness.swe_eval_decision import task_status_row  # noqa: E402
from opencollab.harness.swe_eval_discovery import build_snapshots  # noqa: E402
from opencollab.harness.swe_eval_records import read_bounded_json  # noqa: E402

from scripts import (
    swe_auto_eval_claims,
    swe_auto_eval_constants,
    swe_auto_eval_reports,
    swe_auto_eval_safe_state,
)
from scripts.swe_auto_eval_claims import (
    _acquire_claim,
    _attempt_log_path,
    _attempt_path,
    _claim_is_bootstrapping,
    _claim_is_recent,
    _claim_lease_is_fresh,
    _claim_owner_is_active,
    _claim_path,
    _claim_residual_group_is_live,
    _identity_file_stem,
    _open_append_binary,
    _open_regular_at,
    _open_regular_file,
    _pid_is_active,
    _process_group_exists,
    _process_start_identity,
    _read_json,
    _read_json_at,
    _unlink_durable,
    _validate_side_directory,
    _validate_side_name,
)
from scripts.swe_auto_eval_constants import (
    CLAIM_CONSTRUCTION_GRACE_SECONDS,
    CLAIM_HEARTBEAT_SECONDS,
    CLAIM_LEASE_SECONDS,
    CLAIM_LEGACY_MAX_AGE_SECONDS,
    HARNESS_LOCK_TIMEOUT_SECONDS,
    MAX_CLAIM_BYTES,
    MAX_REPORT_DOCUMENT_BYTES,
    MAX_REPORT_SCAN_BYTES,
    MAX_REPORT_SCAN_ENTRIES,
    MAX_REPORT_SCAN_FILES,
    MAX_SIDE_NAME_BYTES,
    SAFE_FILE_OPEN_RETRIES,
)
from scripts.swe_auto_eval_reports import (
    _iter_report_json_paths,
    _open_real_directory,
    _report_fingerprints,
)
from scripts.swe_auto_eval_safe_state import (
    _acquire_exclusive_lock,
    _fsync_directory,
    _open_secure_parent,
    _stat_at,
    _write_all,
    _write_bytes_atomic,
    _write_bytes_atomic_at,
    _write_json,
    _write_markdown,
)

_EVAL_WRAPPER = (
    "import sys;"
    f"sys.path.insert(0, {str(SCRIPT_ROOT)!r});"
    "from swe_auto_eval_claim_runner import main;"
    "raise SystemExit(main())"
)

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

_COMPATIBILITY_MODULES = (
    swe_auto_eval_constants,
    swe_auto_eval_safe_state,
    swe_auto_eval_claims,
    swe_auto_eval_reports,
)


class _AutoEvalDriverFacade(types.ModuleType):
    """Mirror compatibility patches into focused auto-evaluation helpers."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _AutoEvalDriverFacade


if __name__ == "__main__":
    raise SystemExit(main())
