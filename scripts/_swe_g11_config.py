"""Configuration, report reuse, and adaptive scheduling for the G1.1 runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE_ROOT = "/nfsEDS/dongyh/data/kaka/docker/opencollab"
DEFAULT_EVAL_WORK_ROOT = DEFAULT_REMOTE_ROOT + "/eval_work"
DEFAULT_MODEL_NAME = "opencollab-glm52-g11-16m-prolite"
ALLOWED_WORKFLOW_ENV_KEYS = frozenset(
    {
        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
        "OPENCOLLAB_TEMPERATURE",
        "OPENCOLLAB_THINKING",
        "OPENCOLLAB_THINKING_PARAMS",
        "OPENCOLLAB_TOP_P",
    }
)


@dataclass(frozen=True)
class ParallelConfig:
    indices: tuple[int, ...]
    max_workers: int
    min_workers: int
    adaptive_concurrency: bool
    adaptive_recovery_tasks: int
    run_id: str
    output_dir: Path
    remote_base: str
    remote_runtime_repo: str
    model_name: str
    llm_model: str
    context_window: int | None
    temperature: float | None
    top_p: float | None
    max_output_tokens: int | None
    session_prefix: str
    host: str
    ssh_command: str
    remote_root: str
    workflow: str
    workflow_env: tuple[str, ...]
    openhands_command: str
    openhands_empty_patch_rejections: int
    max_empty_patch_retries: int
    remote_proxy_base_url: str
    local_proxy_base_url: str
    proxy_env_file: Path
    budget: int
    max_steps: int
    swe_timeout: int
    task_wall_timeout: int
    eval_timeout: int
    llm_timeout: int
    checkpoint_interval: int
    max_task_starts: int
    max_eval_attempts: int
    total_timeout: int
    runner_attempts: int
    retry_delay_seconds: int
    usd_cny: float | None
    no_sync_runtime: bool
    no_ensure_remote_proxy: bool
    skip_preflight: bool
    skip_health_checks: bool
    dry_run: bool


@dataclass
class SchedulerState:
    current_workers: int
    clean_streak: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


def _safe_slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    value = value.strip("_.-")
    return value or "run"


def _openhands_command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest() if command else ""


def parse_indices(args: argparse.Namespace) -> tuple[int, ...]:
    if args.indices:
        values = []
        for item in args.indices.split(","):
            item = item.strip()
            if not item:
                continue
            if "-" in item:
                start_text, end_text = item.split("-", 1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if end < start:
                    raise ValueError(f"invalid descending index range: {item}")
                values.extend(range(start, end + 1))
            else:
                values.append(int(item))
        if not values:
            raise ValueError("--indices did not contain any task index")
        return tuple(sorted(dict.fromkeys(values)))
    if args.start_index is None or args.end_index is None:
        raise ValueError("pass either --indices or both --start-index and --end-index")
    if args.end_index < args.start_index:
        raise ValueError("--end-index must be greater than or equal to --start-index")
    return tuple(range(args.start_index, args.end_index + 1))


def range_label(indices: tuple[int, ...]) -> str:
    if not indices:
        return "empty"
    parts: list[str] = []
    start = prev = indices[0]
    for index in indices[1:]:
        if index == prev + 1:
            prev = index
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = index
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def normalize_workflow_env(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: dict[str, str] = {}
    for item in values:
        key, separator, value = str(item).partition("=")
        if not separator or key not in ALLOWED_WORKFLOW_ENV_KEYS:
            raise ValueError(f"unsupported --workflow-env: {item}")
        normalized[key] = value
    return tuple(f"{key}={value}" for key, value in normalized.items())


def default_run_id(indices: tuple[int, ...]) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"swe_g11_prolite_{_safe_slug(range_label(indices))}_{stamp}"


def resolve_config(args: argparse.Namespace) -> ParallelConfig:
    indices = parse_indices(args)
    run_id = _safe_slug(args.run_id or default_run_id(indices))
    remote_base = args.remote_base or f"{args.remote_eval_work_root.rstrip('/')}/{run_id}"
    remote_runtime_repo = args.remote_runtime_repo or f"{remote_base}/_runtime/repo"
    output_dir = args.output_dir or (REPO / "docs" / "monitoring" / run_id)
    session_prefix = args.session_prefix or run_id
    max_workers = max(1, args.max_workers)
    min_workers = min(max_workers, max(1, args.min_workers))
    return ParallelConfig(
        indices=indices,
        max_workers=max_workers,
        min_workers=min_workers,
        adaptive_concurrency=not args.no_adaptive_concurrency,
        adaptive_recovery_tasks=max(1, args.adaptive_recovery_tasks),
        run_id=run_id,
        output_dir=Path(output_dir),
        remote_base=remote_base,
        remote_runtime_repo=remote_runtime_repo,
        model_name=args.model_name,
        llm_model=getattr(args, "llm_model", ""),
        context_window=getattr(args, "context_window", None),
        temperature=getattr(args, "temperature", None),
        top_p=getattr(args, "top_p", None),
        max_output_tokens=getattr(args, "max_output_tokens", None),
        session_prefix=session_prefix,
        host=args.host,
        ssh_command=args.ssh_command,
        remote_root=args.remote_root,
        workflow=args.workflow,
        workflow_env=normalize_workflow_env(getattr(args, "workflow_env", ())),
        openhands_command=getattr(args, "openhands_command", ""),
        openhands_empty_patch_rejections=max(
            0, getattr(args, "openhands_empty_patch_rejections", 2)
        ),
        max_empty_patch_retries=min(
            1, max(0, getattr(args, "max_empty_patch_retries", 1))
        ),
        remote_proxy_base_url=args.remote_proxy_base_url,
        local_proxy_base_url=args.local_proxy_base_url,
        proxy_env_file=args.proxy_env_file,
        budget=args.budget,
        max_steps=args.max_steps,
        swe_timeout=args.swe_timeout,
        task_wall_timeout=args.task_wall_timeout,
        eval_timeout=args.eval_timeout,
        llm_timeout=args.llm_timeout,
        checkpoint_interval=args.checkpoint_interval,
        max_task_starts=max(1, min(3, args.max_task_starts)),
        max_eval_attempts=max(1, min(2, args.max_eval_attempts)),
        total_timeout=args.total_timeout,
        runner_attempts=max(1, args.runner_attempts),
        retry_delay_seconds=max(0, args.retry_delay_seconds),
        usd_cny=args.usd_cny,
        no_sync_runtime=args.no_sync_runtime,
        no_ensure_remote_proxy=args.no_ensure_remote_proxy,
        skip_preflight=args.skip_preflight,
        skip_health_checks=args.skip_health_checks,
        dry_run=args.dry_run,
    )


def _snapshot_evidence_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    object_id = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
    return bool(
        value.get("enabled") is True
        and object_id.fullmatch(str(value.get("anonymous_head") or ""))
        and object_id.fullmatch(str(value.get("base_tree") or ""))
        and value.get("commit_count") == 1
        and value.get("remote_count") == 0
        and value.get("extra_git_metadata") == 0
        and isinstance(value.get("removed_git_metadata"), int)
    )


def report_is_reusable(
    summary: dict[str, Any], config: ParallelConfig, expected_index: int
) -> bool:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    rows = summary.get("rows") if isinstance(summary.get("rows"), list) else []
    tasks = int(counts.get("tasks") or 0)
    if tasks != 1 or len(rows) != tasks:
        return False
    expected_identity = {
        "workflow": config.workflow,
        "model_name": config.model_name,
        "llm_model": config.llm_model,
        "context_window": config.context_window,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_output_tokens,
        "budget": config.budget,
        "max_steps": config.max_steps,
        "max_task_starts": config.max_task_starts,
        "max_empty_patch_retries": getattr(config, "max_empty_patch_retries", 1),
        "max_eval_attempts": config.max_eval_attempts,
        "eval_only": False,
        "solver_attribution": "current_run",
        "remote_runtime_repo": config.remote_runtime_repo,
        "base_run_dir": f"{config.remote_base}/task_{expected_index}",
    }
    if config.workflow == "openhands-external":
        expected_identity["openhands_empty_patch_rejections"] = getattr(
            config, "openhands_empty_patch_rejections", 2
        )
        expected_identity["openhands_command_sha256"] = _openhands_command_sha256(
            getattr(config, "openhands_command", "")
        )
    if any(summary.get(key) != value for key, value in expected_identity.items()):
        return False
    expected_workflow_env = {
        key: value
        for item in config.workflow_env
        for key, _, value in [item.partition("=")]
    }
    if summary.get("workflow_env") != expected_workflow_env:
        return False
    if summary.get("status") != "done" or tasks <= 0:
        return False
    if int(counts.get("technical_failed") or 0) != 0:
        return False
    completed_generation = int(counts.get("generation_done") or 0) + int(
        counts.get("empty_patch") or 0
    )
    completed_eval_or_empty = int(counts.get("eval_done") or 0) + int(
        counts.get("empty_patch") or 0
    )
    if completed_generation != tasks:
        return False
    if completed_eval_or_empty != tasks:
        return False
    for row in rows:
        try:
            row_index = int(row.get("index"))
        except (TypeError, ValueError):
            return False
        if row_index != expected_index or not str(row.get("task") or "").strip():
            return False
        generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
        evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
        if generation.get("status") not in {"generation_done", "empty_patch"}:
            return False
        if config.workflow == "openhands-external" and not _snapshot_evidence_valid(
            generation.get("solver_git_snapshot")
        ):
            return False
        expected_eval = (
            "skipped_empty_patch"
            if generation.get("status") == "empty_patch"
            else "eval_done"
        )
        if evaluation.get("status") != expected_eval:
            return False
    return True


def normalize_legacy_empty_patch_summary(
    summary: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    rows = summary.get("rows") if isinstance(summary.get("rows"), list) else []
    if len(rows) != 1:
        return summary, False
    generation = (
        rows[0].get("generation")
        if isinstance(rows[0].get("generation"), dict)
        else {}
    )
    evaluation = rows[0].get("eval") if isinstance(rows[0].get("eval"), dict) else {}
    if not (
        generation.get("workflow_status") == "empty_patch_after_done"
        and int(generation.get("patch_len") or 0) == 0
        and evaluation.get("status") == "skipped_no_generation_patch"
    ):
        return summary, False
    normalized = json.loads(json.dumps(summary))
    normalized["status"] = "done"
    normalized["rows"][0]["generation"]["status"] = "empty_patch"
    normalized["rows"][0]["eval"]["status"] = "skipped_empty_patch"
    counts = normalized.setdefault("counts", {})
    counts["empty_patch"] = 1
    counts["technical_failed"] = 0
    return normalized, True


RESOURCE_RUNNER_STATUSES = {
    "missing_report",
    "orchestrator_exception",
}

RESOURCE_GENERATION_STATUSES = {
    "blocked_bad_generation_workdir",
    "blocked_missing_generation_image",
    "fifo_write_failed",
    "generation_timeout",
}

RESOURCE_EVAL_STATUSES = {
    "blocked_missing_eval_image",
}

RESOURCE_TECHNICAL_REASONS = {
    "docker_exit",
    "fail_to_pass_infra",
    "pass_to_pass_infra",
}

RESOURCE_TEXT_PATTERNS = (
    "connection refused",
    "database is locked",
    "docker",
    "econnrefused",
    "missing report",
    "no space left",
    "rsync",
    "serverselectiontimeouterror",
    "ssh",
    "timed out",
    "timeout",
)

RETRYABLE_TASK_REPORT_STATUSES = {"preflight_failed"}


def unique_strings(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def text_resource_reasons(text: str) -> list[str]:
    lower = text.lower()
    return [f"text:{pattern}" for pattern in RESOURCE_TEXT_PATTERNS if pattern in lower]


def result_resource_reasons(result: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    runner_status = str(result.get("runner_status") or "")
    if runner_status in RESOURCE_RUNNER_STATUSES:
        reasons.append(f"runner:{runner_status}")
    if result.get("returncode") not in (None, 0, 1) and not result.get("completed"):
        reasons.append("runner:nonterminal_return")
    runner_failed = runner_status in RESOURCE_RUNNER_STATUSES or (
        result.get("returncode") not in (None, 0, 1) and not result.get("completed")
    )
    if runner_failed or result.get("error"):
        reasons.extend(
            text_resource_reasons(
                " ".join(
                    str(result.get(key) or "")
                    for key in ("error", "stderr_tail", "runner_status")
                )
            )
        )
    for row in result.get("rows") or []:
        if not isinstance(row, dict):
            continue
        generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
        generation_status = str(generation.get("status") or "")
        if generation_status in RESOURCE_GENERATION_STATUSES:
            reasons.append(f"generation:{generation_status}")
        eval_result = row.get("eval") if isinstance(row.get("eval"), dict) else {}
        eval_status = str(eval_result.get("status") or "")
        if eval_status in RESOURCE_EVAL_STATUSES:
            reasons.append(f"eval:{eval_status}")
        for attempt in [eval_result, *(eval_result.get("attempts") or [])]:
            if not isinstance(attempt, dict):
                continue
            attempt_status = str(attempt.get("status") or "")
            summary = attempt.get("summary") if isinstance(attempt.get("summary"), dict) else {}
            summary_status = str(summary.get("status") or "")
            attempt_technical = (
                attempt_status == "technical_eval_failed"
                or summary_status == "technical_eval_failed"
            )
            for reason in summary.get("technical_reasons") or []:
                if str(reason) in RESOURCE_TECHNICAL_REASONS:
                    reasons.append(f"eval:{reason}")
                    attempt_technical = True
            if attempt_technical:
                tests_status = (
                    summary.get("tests_status")
                    if isinstance(summary.get("tests_status"), dict)
                    else {}
                )
                for key in ("f2p_log_tail", "p2p_log_tail"):
                    reasons.extend(text_resource_reasons(str(tests_status.get(key) or "")))
    return unique_strings(reasons)


def update_scheduler_state(
    config: ParallelConfig, state: SchedulerState, result: dict[str, Any]
) -> None:
    if not config.adaptive_concurrency:
        return
    reasons = result_resource_reasons(result)
    index = result.get("index")
    if reasons:
        old_workers = state.current_workers
        state.clean_streak = 0
        state.current_workers = max(config.min_workers, state.current_workers - 1)
        state.events.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                "index": index,
                "action": "decrease" if state.current_workers < old_workers else "hold_min",
                "old_workers": old_workers,
                "new_workers": state.current_workers,
                "reasons": reasons,
            }
        )
        return
    state.clean_streak += 1
    if (
        state.current_workers < config.max_workers
        and state.clean_streak >= config.adaptive_recovery_tasks
    ):
        old_workers = state.current_workers
        state.current_workers += 1
        state.clean_streak = 0
        state.events.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                "index": index,
                "action": "increase",
                "old_workers": old_workers,
                "new_workers": state.current_workers,
                "reasons": ["clean_streak"],
            }
        )


def scheduler_snapshot(
    config: ParallelConfig,
    state: SchedulerState,
    pending: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "adaptive_concurrency": config.adaptive_concurrency,
        "current_workers": state.current_workers,
        "max_workers": config.max_workers,
        "min_workers": config.min_workers,
        "clean_streak": state.clean_streak,
        "adaptive_recovery_tasks": config.adaptive_recovery_tasks,
        "pending": pending or [],
        "events": state.events[-50:],
    }
