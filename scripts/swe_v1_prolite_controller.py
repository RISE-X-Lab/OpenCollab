"""Top-level remote execution controller for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.swe_v1_prolite_common import (
    MAX_TOTAL_EVAL_ATTEMPTS,
    REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS,
    REMOTE_TERMINAL_STATUSES,
    REPO_ROOT,
    _redacted,
)
from scripts.swe_v1_prolite_config import (
    ensure_remote_proxy,
    get_proxy_token,
    normalize_workflow_env,
    sync_runtime,
)
from scripts.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _bounded_remote_communicate,
    _cleanup_remote_execution,
    _local_process_group_exists,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)


def probe_terminal_remote_summary(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
) -> dict[str, Any] | None:
    probe = r'''import json
import os
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
summary_path = base / "summary.json"
runner_pid_path = base / "runner.pid"
try:
    runner_pid = int(runner_pid_path.read_text(encoding="utf-8").strip())
except Exception:
    runner_pid = 0
runner_alive = False
if runner_pid > 1:
    try:
        os.kill(runner_pid, 0)
        runner_alive = True
    except ProcessLookupError:
        pass
    except PermissionError:
        runner_alive = True
try:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
except Exception:
    summary = None
print(json.dumps({"runner_alive": runner_alive, "summary": summary}, ensure_ascii=False))
'''
    command = [
        *ssh_command,
        host,
        "python3 -c " + shlex.quote(probe) + " " + shlex.quote(base_run_dir),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    summary = observed.get("summary")
    if observed.get("runner_alive") or not isinstance(summary, dict):
        return None
    if summary.get("status") not in REMOTE_TERMINAL_STATUSES:
        return None
    return summary


def remote_summary_matches_payload(
    summary: dict[str, Any], payload: dict[str, Any]
) -> bool:
    start_index = int(payload["start_index"])
    end_index = start_index + max(int(payload["limit"]), 0) - 1
    expected_slice = (
        str(start_index)
        if end_index <= start_index
        else f"{start_index}-{end_index}"
    )
    expected = {
        "slice": expected_slice,
        "base_run_dir": payload["base_run_dir"],
        "remote_runtime_repo": payload["remote_repo"],
        "invocation_id": payload["invocation_id"],
        "workflow": payload["workflow"],
        "workflow_env": payload["workflow_env"],
        "model_name": payload["model_name"],
        "llm_model": payload["llm_model"],
        "llm_provider": payload["llm_provider"],
        "context_window": payload["context_window"],
        "temperature": payload["temperature"],
        "top_p": payload["top_p"],
        "max_output_tokens": payload["max_output_tokens"],
        "budget": payload["budget"],
        "max_steps": payload["max_steps"],
        "max_task_starts": max(1, min(3, int(payload["max_task_starts"]))),
        "max_empty_patch_retries": min(
            1, max(0, int(payload["max_empty_patch_retries"]))
        ),
        "max_eval_attempts": min(2, max(1, int(payload["max_eval_attempts"]))),
        "eval_only": payload["eval_only"],
        "eval_dir_name": payload["eval_dir_name"],
        "solver_attribution": (
            "historical_artifact" if payload["eval_only"] else "current_run"
        ),
    }
    if payload.get("workflow") == "openhands-external":
        expected["openhands_empty_patch_rejections"] = max(
            0, int(payload["openhands_empty_patch_rejections"])
        )
        expected["openhands_command_sha256"] = hashlib.sha256(
            payload["openhands_command"].encode("utf-8")
        ).hexdigest()
    return all(summary.get(key) == value for key, value in expected.items())


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    defaults = {
        "workflow_env": [],
        "openhands_command": "",
        "openhands_empty_patch_rejections": 2,
        "max_empty_patch_retries": 1,
        "llm_model": "",
        "llm_provider": "anthropic",
        "context_window": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "max_eval_attempts": 2,
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    ssh_command = shlex.split(args.ssh_command)
    eval_only = bool(getattr(args, "eval_only", False))
    eval_dir_name = str(getattr(args, "eval_dir_name", "official_eval"))
    if eval_only:
        proxy_summary = {
            "status": "skipped_eval_only",
            "remote_proxy_base_url": args.remote_proxy_base_url,
        }
        sync_summary = {}
    else:
        proxy_summary = ensure_remote_proxy(
            ssh_command=ssh_command,
            host=args.host,
            local_proxy_base_url=args.local_proxy_base_url,
            remote_proxy_base_url=args.remote_proxy_base_url,
            enabled=not args.no_ensure_remote_proxy,
        )
        sync_summary = (
            {}
            if args.no_sync_runtime
            else sync_runtime(
            ssh_command=ssh_command,
            host=args.host,
            remote_runtime_repo=args.remote_runtime_repo,
            )
        )
    selected_remote_proxy_base_url = proxy_summary.get("remote_proxy_base_url", args.remote_proxy_base_url)
    owner_nonce = uuid.uuid4().hex
    payload = {
        "token": "" if eval_only else get_proxy_token(args.proxy_env_file),
        "owner_nonce": owner_nonce,
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "base_run_dir": args.base_run_dir,
        "workflow": args.workflow,
        "workflow_env": normalize_workflow_env(args.workflow_env),
        "openhands_command": args.openhands_command,
        "openhands_empty_patch_rejections": max(
            0, args.openhands_empty_patch_rejections
        ),
        "max_empty_patch_retries": min(
            1, max(0, args.max_empty_patch_retries)
        ),
        "model_name": args.model_name,
        "llm_model": args.llm_model,
        "llm_provider": args.llm_provider,
        "context_window": args.context_window,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_output_tokens": args.max_output_tokens,
        "invocation_id": uuid.uuid4().hex,
        "session_prefix": args.session_prefix,
        "image_repository": args.image_repository,
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
        "max_eval_attempts": args.max_eval_attempts,
        "eval_only": eval_only,
        "eval_dir_name": eval_dir_name,
        "dry_run": args.dry_run,
    }
    remote_pythonpath = str(Path(args.remote_runtime_repo) / "opencollab")
    remote_command = (
        "env PYTHONPATH="
        + shlex.quote(remote_pythonpath)
        + " python3 -m opencollab.harness.swe_v1_remote_runner "
        + shlex.quote(owner_nonce)
    )
    command = [*ssh_command, args.host, remote_command]
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
        recovered_summary = probe_terminal_remote_summary(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
        )
        if recovered_summary is not None and remote_summary_matches_payload(
            recovered_summary,
            payload,
        ):
            local_quiesced = terminate_local_process_group(proc)
            if not local_quiesced:
                raise RuntimeError(
                    "remote summary completed but the local SSH process group "
                    "did not quiesce"
                ) from exc
            recovered_summary["remote_transport"] = {
                "status": "recovered_terminal_summary",
                "base_run_dir": args.base_run_dir,
            }
            recovered_summary["runtime_sync"] = sync_summary
            recovered_summary["remote_proxy"] = proxy_summary
            return recovered_summary
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            proc=proc,
        )
        if interruption is not None:
            raise interruption
        raise RuntimeError(f"remote run timed out after {args.total_timeout}s; cleanup={cleanup}") from exc
    except BaseException:
        cleanup, _interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            proc=proc,
        )
        print(
            "remote execution aborted; cleanup requested: " + json.dumps(cleanup, ensure_ascii=False),
            file=sys.stderr,
        )
        raise
    if _local_process_group_exists(proc.pid):
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            proc=proc,
        )
        if interruption is not None:
            raise interruption
        if not cleanup.get("ok"):
            raise RuntimeError(
                f"ssh leader exited with residual process-group descendants; technical cleanup failure: {cleanup}"
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

def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    for result in report.get("results") or []:
        if isinstance(result, dict):
            rows.extend(row for row in result.get("rows") or [] if isinstance(row, dict))
    return rows


def _eval_result_executed(result: dict[str, Any]) -> bool:
    if result.get("executed") is False:
        return False
    return str(result.get("status") or "") not in {
        "",
        "would_eval",
        "skipped_no_generation_patch",
        "blocked_missing_eval_image",
    }


def _row_eval_attempt_count(row: dict[str, Any]) -> int:
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    attempts = evaluation.get("attempts")
    if isinstance(attempts, list) and attempts:
        return sum(
            _eval_result_executed(attempt)
            for attempt in attempts
            if isinstance(attempt, dict)
        )
    if not _eval_result_executed(evaluation):
        return 0
    try:
        count = int(evaluation.get("attempt_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count or 1


def _report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in _report_rows(report):
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        counts[index] = counts.get(index, 0) + _row_eval_attempt_count(row)
    return counts


def _final_report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        try:
            index = int(task.get("index"))
            count = int(
                task.get("observed_eval_attempt_count")
                or task.get("eval_attempt_count")
                or 0
            )
        except (TypeError, ValueError):
            continue
        counts[index] = max(counts.get(index, 0), count)
    return counts


class ParentEvalLock:
    def __init__(self, parent_output_dir: Path):
        self.path = parent_output_dir.resolve() / ".eval_only.lock"
        self.handle: Any | None = None

    def __enter__(self) -> "ParentEvalLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def parent_eval_lock(args: argparse.Namespace) -> ParentEvalLock:
    if not args.eval_only or args.parent_output_dir is None:
        raise RuntimeError("eval-only runs require a parent output directory")
    return ParentEvalLock(args.parent_output_dir)


def apply_parent_eval_budget(args: argparse.Namespace) -> dict[str, Any] | None:
    if not (args.eval_only and args.parent_output_dir):
        return None
    parent_summary = args.parent_output_dir.resolve() / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")
    report = json.loads(parent_summary.read_text(encoding="utf-8", errors="replace"))
    counts_by_index = _report_task_eval_counts(report)
    final_report_path = args.parent_output_dir.resolve() / "final_eval_layer_report.json"
    final_report_counts: dict[int, int] = {}
    if final_report_path.exists():
        final_report = json.loads(
            final_report_path.read_text(encoding="utf-8", errors="replace")
        )
        final_report_counts = _final_report_task_eval_counts(final_report)
        for index, count in final_report_counts.items():
            counts_by_index[index] = max(counts_by_index.get(index, 0), count)
    selected = range(args.start_index, args.start_index + max(args.limit, 0))
    remaining_by_index = {
        index: MAX_TOTAL_EVAL_ATTEMPTS - counts_by_index.get(index, 0)
        for index in selected
    }
    exhausted = [index for index, remaining in remaining_by_index.items() if remaining <= 0]
    if exhausted:
        joined = ", ".join(str(index) for index in exhausted)
        raise RuntimeError(
            "eval retry budget exhausted for task indices: "
            f"{joined}; max total is {MAX_TOTAL_EVAL_ATTEMPTS}"
        )
    effective_max_attempts = min(args.max_eval_attempts, *remaining_by_index.values())
    args.max_eval_attempts = effective_max_attempts
    return {
        "max_total_eval_attempts": MAX_TOTAL_EVAL_ATTEMPTS,
        "previous_eval_attempts": counts_by_index,
        "final_report_eval_attempts": final_report_counts,
        "remaining_by_index": remaining_by_index,
        "effective_max_eval_attempts": effective_max_attempts,
    }


def update_parent_fact_report(args: argparse.Namespace) -> dict[str, Any]:
    parent_output_dir = args.parent_output_dir.resolve()
    parent_summary = parent_output_dir / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "swe_eval_layer_report.py"),
        "--report-json",
        str(parent_summary),
        "--report-json",
        str(args.json_output.resolve()),
        "--max-rounds",
        "2",
        "--allow-over-budget-evidence",
        "--json-output",
        str(parent_output_dir / "final_eval_layer_report.json"),
        "--markdown-output",
        str(parent_output_dir / "final_eval_layer_report.md"),
    ]
    token_cost = parent_output_dir / "parallel_token_cost_summary.json"
    if token_cost.exists():
        command.extend(["--token-cost-json", str(token_cost)])
    if args.usd_cny is not None:
        command.extend(["--usd-cny", str(args.usd_cny)])
    proc = subprocess.run(command, text=True, capture_output=True, cwd=REPO_ROOT)
    log_path = parent_output_dir / "eval_only_reconciliation.log"
    log_path.write_text(proc.stdout + proc.stderr, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"parent fact report failed rc={proc.returncode}; see {log_path}")
    report_path = parent_output_dir / "final_eval_layer_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
    return {
        "status": "done",
        "report_json": str(report_path),
        "report_markdown": str(parent_output_dir / "final_eval_layer_report.md"),
        "counts": report.get("counts") if isinstance(report, dict) else {},
    }


__all__ = [name for name in globals() if not name.startswith("__")]
