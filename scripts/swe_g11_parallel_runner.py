#!/usr/bin/env python3
"""Parameterized G1.1 Pro-Lite parallel runner."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    from scripts import _swe_g11_config as _config
    from scripts import _swe_g11_reports as _reports
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    import _swe_g11_config as _config  # type: ignore[no-redef]
    import _swe_g11_reports as _reports  # type: ignore[no-redef]


# Preserve the original import surface while keeping implementation modules focused.
REPO = _config.REPO
DEFAULT_REMOTE_ROOT = _config.DEFAULT_REMOTE_ROOT
DEFAULT_EVAL_WORK_ROOT = _config.DEFAULT_EVAL_WORK_ROOT
DEFAULT_MODEL_NAME = _config.DEFAULT_MODEL_NAME
ALLOWED_WORKFLOW_ENV_KEYS = _config.ALLOWED_WORKFLOW_ENV_KEYS
ParallelConfig = _config.ParallelConfig
SchedulerState = _config.SchedulerState
RESOURCE_RUNNER_STATUSES = _config.RESOURCE_RUNNER_STATUSES
RESOURCE_GENERATION_STATUSES = _config.RESOURCE_GENERATION_STATUSES
RESOURCE_EVAL_STATUSES = _config.RESOURCE_EVAL_STATUSES
RESOURCE_TECHNICAL_REASONS = _config.RESOURCE_TECHNICAL_REASONS
RESOURCE_TEXT_PATTERNS = _config.RESOURCE_TEXT_PATTERNS
RETRYABLE_TASK_REPORT_STATUSES = _config.RETRYABLE_TASK_REPORT_STATUSES
_safe_slug = _config._safe_slug
_openhands_command_sha256 = _config._openhands_command_sha256
_snapshot_evidence_valid = _config._snapshot_evidence_valid
parse_indices = _config.parse_indices
range_label = _config.range_label
normalize_workflow_env = _config.normalize_workflow_env
default_run_id = _config.default_run_id
resolve_config = _config.resolve_config
report_is_reusable = _config.report_is_reusable
normalize_legacy_empty_patch_summary = _config.normalize_legacy_empty_patch_summary
unique_strings = _config.unique_strings
text_resource_reasons = _config.text_resource_reasons
result_resource_reasons = _config.result_resource_reasons
update_scheduler_state = _config.update_scheduler_state
scheduler_snapshot = _config.scheduler_snapshot

write_json = _reports.write_json
load_json = _reports.load_json
_compact_token_summary = _reports._compact_token_summary
build_token_summary = _reports.build_token_summary
build_eval_fact_report = _reports.build_eval_fact_report
aggregate = _reports.aggregate
compact_progress = _reports.compact_progress
write_markdown = _reports.write_markdown
save_progress = _reports.save_progress


def task_paths(config: ParallelConfig, index: int) -> dict[str, Path]:
    return {
        "json_report": config.output_dir / f"task_{index}_report.json",
        "markdown_report": config.output_dir / f"task_{index}_report.md",
        "stdout_log": config.output_dir / f"task_{index}.stdout.log",
        "stderr_log": config.output_dir / f"task_{index}.stderr.log",
    }


def task_command(config: ParallelConfig, index: int) -> list[str]:
    paths = task_paths(config, index)
    command = [
        sys.executable,
        "scripts/swe_g11_prolite_runner.py",
        "--host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--remote-root",
        config.remote_root,
        "--run-id",
        f"{config.run_id}_task{index}",
        "--session-prefix",
        config.session_prefix,
        "--model-name",
        config.model_name,
        "--start-index",
        str(index),
        "--limit",
        "1",
        "--base-run-dir",
        f"{config.remote_base}/task_{index}",
        "--remote-runtime-repo",
        config.remote_runtime_repo,
        "--workflow",
        config.workflow,
        "--remote-proxy-base-url",
        config.remote_proxy_base_url,
        "--local-proxy-base-url",
        config.local_proxy_base_url,
        "--proxy-env-file",
        str(config.proxy_env_file),
        "--budget",
        str(config.budget),
        "--max-steps",
        str(config.max_steps),
        "--openhands-empty-patch-rejections",
        str(config.openhands_empty_patch_rejections),
        "--max-empty-patch-retries",
        str(config.max_empty_patch_retries),
        "--swe-timeout",
        str(config.swe_timeout),
        "--task-wall-timeout",
        str(config.task_wall_timeout),
        "--eval-timeout",
        str(config.eval_timeout),
        "--llm-timeout",
        str(config.llm_timeout),
        "--checkpoint-interval",
        str(config.checkpoint_interval),
        "--max-task-starts",
        str(config.max_task_starts),
        "--max-eval-attempts",
        str(config.max_eval_attempts),
        "--total-timeout",
        str(config.total_timeout),
        "--json-output",
        str(paths["json_report"]),
        "--markdown-output",
        str(paths["markdown_report"]),
    ]
    for option, value in (
        ("--llm-model", config.llm_model),
        ("--context-window", config.context_window),
        ("--temperature", config.temperature),
        ("--top-p", config.top_p),
        ("--max-output-tokens", config.max_output_tokens),
    ):
        if value not in (None, ""):
            command += [option, str(value)]
    for item in config.workflow_env:
        command += ["--workflow-env", item]
    if config.openhands_command:
        command += ["--openhands-command", config.openhands_command]
    if config.no_sync_runtime:
        command.append("--no-sync-runtime")
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    if config.dry_run:
        command.append("--dry-run")
    return command


def task_result_from_summary(
    config: ParallelConfig,
    index: int,
    summary: dict[str, Any],
    *,
    reused: bool,
    elapsed: float,
) -> dict[str, Any]:
    paths = task_paths(config, index)
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    return {
        "index": index,
        "returncode": 0 if summary.get("status") == "done" else 1,
        "elapsed_seconds": round(elapsed, 1),
        "json_report": str(paths["json_report"]),
        "markdown_report": str(paths["markdown_report"]),
        "stdout_log": str(paths["stdout_log"]),
        "stderr_log": str(paths["stderr_log"]),
        "runner_status": summary.get("status"),
        "tasks": counts.get("tasks", 0),
        "generation_done": counts.get("generation_done", 0),
        "empty_patch": counts.get("empty_patch", 0),
        "eval_done": counts.get("eval_done", 0),
        "eval_attempts": counts.get("eval_attempts", 0),
        "eval_retry_tasks": counts.get("eval_retry_tasks", 0),
        "resolved": counts.get("resolved", 0),
        "unresolved": counts.get("unresolved", 0),
        "technical_failed": counts.get("technical_failed", 0),
        "rows": summary.get("rows") if isinstance(summary.get("rows"), list) else [],
        "completed": bool(summary),
        "reused_existing_report": reused,
    }


def run_one(config: ParallelConfig, index: int) -> dict[str, Any]:
    started = time.time()
    paths = task_paths(config, index)
    if paths["json_report"].exists():
        summary = load_json(paths["json_report"])
        summary, normalized = normalize_legacy_empty_patch_summary(summary)
        if normalized:
            paths["json_report"].write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if report_is_reusable(summary, config, index):
            return task_result_from_summary(
                config,
                index,
                summary,
                reused=True,
                elapsed=0.0,
            )

    paths["stdout_log"].parent.mkdir(parents=True, exist_ok=True)
    proc: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, config.runner_attempts + 1):
        proc = subprocess.run(
            task_command(config, index),
            cwd=REPO,
            text=True,
            capture_output=True,
        )
        paths["stdout_log"].write_text(
            proc.stdout,
            encoding="utf-8",
            errors="replace",
        )
        paths["stderr_log"].write_text(
            proc.stderr,
            encoding="utf-8",
            errors="replace",
        )
        summary = load_json(paths["json_report"])
        if summary:
            result = task_result_from_summary(
                config,
                index,
                summary,
                reused=False,
                elapsed=time.time() - started,
            )
            result["returncode"] = proc.returncode
            result["attempts"] = attempt
            if (
                str(summary.get("status") or "") not in RETRYABLE_TASK_REPORT_STATUSES
                or attempt >= config.runner_attempts
            ):
                return result
        if attempt < config.runner_attempts and config.retry_delay_seconds:
            time.sleep(config.retry_delay_seconds * attempt)

    assert proc is not None
    return {
        "index": index,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 1),
        "json_report": str(paths["json_report"]),
        "markdown_report": str(paths["markdown_report"]),
        "stdout_log": str(paths["stdout_log"]),
        "stderr_log": str(paths["stderr_log"]),
        "runner_status": "missing_report",
        "completed": False,
        "attempts": config.runner_attempts,
    }


def prepare_runtime(config: ParallelConfig) -> None:
    if config.skip_preflight:
        return
    preflight_json = config.output_dir / "shared_runtime_preflight.json"
    preflight_md = config.output_dir / "shared_runtime_preflight.md"
    preflight_run_dir = f"{config.remote_base}/_preflight"
    command = [
        sys.executable,
        "scripts/swe_g11_prolite_runner.py",
        "--host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--remote-root",
        config.remote_root,
        "--run-id",
        f"{config.run_id}_preflight",
        "--session-prefix",
        config.session_prefix,
        "--model-name",
        config.model_name,
        "--start-index",
        str(config.indices[0]),
        "--limit",
        "1",
        "--base-run-dir",
        preflight_run_dir,
        "--remote-runtime-repo",
        config.remote_runtime_repo,
        "--workflow",
        config.workflow,
        "--remote-proxy-base-url",
        config.remote_proxy_base_url,
        "--local-proxy-base-url",
        config.local_proxy_base_url,
        "--proxy-env-file",
        str(config.proxy_env_file),
        "--budget",
        str(config.budget),
        "--max-steps",
        str(config.max_steps),
        "--json-output",
        str(preflight_json),
        "--markdown-output",
        str(preflight_md),
        "--dry-run",
    ]
    for option, value in (
        ("--llm-model", config.llm_model),
        ("--context-window", config.context_window),
        ("--temperature", config.temperature),
        ("--top-p", config.top_p),
        ("--max-output-tokens", config.max_output_tokens),
    ):
        if value not in (None, ""):
            command += [option, str(value)]
    for item in config.workflow_env:
        command += ["--workflow-env", item]
    if config.openhands_command:
        command += ["--openhands-command", config.openhands_command]
    if config.no_sync_runtime:
        command.append("--no-sync-runtime")
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    proc = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    (config.output_dir / "shared_runtime_preflight.stdout.log").write_text(
        proc.stdout,
        encoding="utf-8",
        errors="replace",
    )
    (config.output_dir / "shared_runtime_preflight.stderr.log").write_text(
        proc.stderr,
        encoding="utf-8",
        errors="replace",
    )
    summary = load_json(preflight_json)
    if proc.returncode != 0 or summary.get("status") != "dry_run":
        raise RuntimeError(
            f"shared runtime preflight failed rc={proc.returncode} "
            f"status={summary.get('status')}"
        )


def remote_health_script(config: ParallelConfig) -> str:
    remote_base = shlex.quote(config.remote_base)
    remote_runtime_repo = shlex.quote(config.remote_runtime_repo)
    return "\n".join(
        [
            "set -eu",
            f"mkdir -p {remote_base}",
            "command -v python3 >/dev/null",
            "command -v docker >/dev/null",
            "docker info >/dev/null",
            f"df -Pk {remote_base}",
            f"test -d {remote_runtime_repo}",
        ]
    )


def run_remote_health_checks(config: ParallelConfig) -> dict[str, Any]:
    json_path = config.output_dir / "remote_health_check.json"
    stdout_path = config.output_dir / "remote_health_check.stdout.log"
    stderr_path = config.output_dir / "remote_health_check.stderr.log"
    if config.skip_health_checks or config.dry_run:
        result = {
            "status": "skipped",
            "reason": "disabled" if config.skip_health_checks else "dry_run",
        }
        write_json(json_path, result)
        return result
    command = [
        *shlex.split(config.ssh_command),
        config.host,
        "bash -lc " + shlex.quote(remote_health_script(config)),
    ]
    proc = subprocess.run(
        command,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=120,
    )
    stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
    result = {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    write_json(json_path, result)
    if proc.returncode != 0:
        raise RuntimeError(f"remote health check failed rc={proc.returncode}")
    return result


def run_parallel(config: ParallelConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    prepare_runtime(config)
    remote_health = run_remote_health_checks(config)
    per_task_config = config
    if not config.no_sync_runtime or not config.no_ensure_remote_proxy:
        per_task_config = replace(
            config,
            no_sync_runtime=True,
            no_ensure_remote_proxy=True,
        )
    results: list[dict[str, Any]] = []
    futures: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
    pending = list(config.indices)
    scheduler = SchedulerState(current_workers=config.max_workers)

    def current_scheduler() -> dict[str, Any]:
        return scheduler_snapshot(config, scheduler, pending=list(pending))

    def submit_ready(executor: concurrent.futures.ThreadPoolExecutor) -> None:
        while pending and len(futures) < scheduler.current_workers:
            index = pending.pop(0)
            futures[executor.submit(run_one, per_task_config, index)] = index

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.max_workers
    ) as executor:
        submit_ready(executor)
        while futures or pending:
            if not futures:
                submit_ready(executor)
                continue
            done, _ = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                index = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "index": index,
                        "returncode": 99,
                        "elapsed_seconds": 0,
                        "runner_status": "orchestrator_exception",
                        "error": str(exc),
                        "completed": False,
                    }
                results.append(result)
                update_scheduler_state(config, scheduler, result)
                submit_ready(executor)
                save_progress(
                    config,
                    results,
                    sorted(futures.values()),
                    scheduler=current_scheduler(),
                    remote_health=remote_health,
                )
                progress = aggregate(
                    config,
                    results,
                    sorted(futures.values()),
                    scheduler=current_scheduler(),
                    remote_health=remote_health,
                )
                print(
                    json.dumps(compact_progress(progress), ensure_ascii=False),
                    flush=True,
                )
    token_cost = build_token_summary(config)
    save_progress(
        config,
        results,
        token_cost=token_cost,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )
    fact_report = build_eval_fact_report(config)
    save_progress(
        config,
        results,
        token_cost=token_cost,
        fact_report=fact_report,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )
    return aggregate(
        config,
        results,
        token_cost=token_cost,
        fact_report=fact_report,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run G1.1 Pro-Lite tasks in parallel and produce final reports."
    )
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--indices", default="")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--min-workers", type=int, default=1)
    parser.add_argument("--adaptive-recovery-tasks", type=int, default=2)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--remote-base", default="")
    parser.add_argument("--remote-eval-work-root", default=DEFAULT_EVAL_WORK_ROOT)
    parser.add_argument("--remote-runtime-repo", default="")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--session-prefix", default="")
    parser.add_argument("--host", default="jinan-aws")
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--workflow", default="validation-council-solve")
    parser.add_argument("--workflow-env", action="append", default=[])
    parser.add_argument("--openhands-command", default="")
    parser.add_argument("--openhands-empty-patch-rejections", type=int, default=2)
    parser.add_argument("--max-empty-patch-retries", type=int, default=1)
    parser.add_argument("--remote-proxy-base-url", default="http://127.0.0.1:18788")
    parser.add_argument("--local-proxy-base-url", default="http://127.0.0.1:8878")
    parser.add_argument(
        "--proxy-env-file",
        type=Path,
        default=Path.home() / ".claude" / "glm52.env",
    )
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--swe-timeout", type=int, default=14_400)
    parser.add_argument("--task-wall-timeout", type=int, default=15_300)
    parser.add_argument("--eval-timeout", type=int, default=7_200)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--checkpoint-interval", type=int, default=300)
    parser.add_argument("--max-task-starts", type=int, default=3)
    parser.add_argument("--max-eval-attempts", type=int, default=2)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--runner-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=int, default=60)
    parser.add_argument("--usd-cny", type=float)
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-health-checks", action="store_true")
    parser.add_argument("--no-adaptive-concurrency", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = resolve_config(args)
        final = run_parallel(config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(compact_progress(final), ensure_ascii=False, indent=2))
    return 0 if final["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
