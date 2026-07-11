"""Top-level remote execution controller for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.swe_v1_prolite_common import _redacted
from scripts.swe_v1_prolite_config import (
    ensure_remote_proxy,
    get_proxy_token,
    sync_runtime,
)
from scripts.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _bounded_remote_communicate,
    _cleanup_remote_execution,
    _local_process_group_exists,
    _restore_local_spawn_signals,
)


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    ssh_command = shlex.split(args.ssh_command)
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
        "token": get_proxy_token(args.proxy_env_file),
        "owner_nonce": owner_nonce,
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "base_run_dir": args.base_run_dir,
        "workflow": args.workflow,
        "model_name": args.model_name,
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


__all__ = [name for name in globals() if not name.startswith("__")]
