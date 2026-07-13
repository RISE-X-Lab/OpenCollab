"""Configuration, report reuse, and adaptive scheduling for the G1.1 runner."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from scripts import _swe_eval_layer_integrity as _eval_integrity
except ModuleNotFoundError as exc:  # Direct execution adds ``scripts`` rather than the repo root.
    if exc.name != "scripts":
        raise
    import _swe_eval_layer_integrity as _eval_integrity  # type: ignore[no-redef]

from opencollab.harness.swe_eval_records import (  # noqa: E402
    SUBMISSION_INTEGRITY_PROVEN,
    metric_submission_integrity,
)
from opencollab.harness.swe_generation_proof import (  # noqa: E402
    current_generation_summary_proof_valid,
    solver_git_snapshot_valid,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE_ROOT = os.environ.get("OPENCOLLAB_SWE_REMOTE_ROOT", "").strip()
DEFAULT_EVAL_WORK_ROOT = os.environ.get("OPENCOLLAB_SWE_EVAL_WORK_ROOT", "").strip()
if not DEFAULT_EVAL_WORK_ROOT and DEFAULT_REMOTE_ROOT:
    DEFAULT_EVAL_WORK_ROOT = DEFAULT_REMOTE_ROOT.rstrip("/") + "/eval_work"
DEFAULT_MODEL_NAME = os.environ.get("OPENCOLLAB_SWE_MODEL_NAME", "").strip()
DEFAULT_IMAGE_REPOSITORY = os.environ.get(
    "OPENCOLLAB_SWE_IMAGE_REPOSITORY", ""
).strip()
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
    image_repository: str
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
    remote_eval_work_root = str(args.remote_eval_work_root or "").strip()
    remote_base = str(args.remote_base or "").strip()
    if not remote_base:
        if not remote_eval_work_root:
            raise ValueError(
                "pass --remote-base or configure --remote-eval-work-root or "
                "OPENCOLLAB_SWE_EVAL_WORK_ROOT"
            )
        remote_base = f"{remote_eval_work_root.rstrip('/')}/{run_id}"
    remote_runtime_repo = args.remote_runtime_repo or f"{remote_base}/_runtime/repo"
    output_dir = args.output_dir or (REPO / "docs" / "monitoring" / run_id)
    session_prefix = args.session_prefix or run_id
    max_workers = max(1, args.max_workers)
    min_workers = min(max_workers, max(1, args.min_workers))
    host = str(args.host or "").strip()
    remote_root = str(args.remote_root or "").strip()
    image_repository = str(args.image_repository or "").strip()
    model_name = str(args.model_name or "").strip()
    llm_model = str(getattr(args, "llm_model", "") or "").strip()
    workflow = str(args.workflow or "").strip()
    openhands_command = str(getattr(args, "openhands_command", "") or "").strip()
    proxy_env_file = getattr(args, "proxy_env_file", None)
    remote_proxy_base_url = str(args.remote_proxy_base_url or "").strip()
    local_proxy_base_url = str(args.local_proxy_base_url or "").strip()
    required = {
        "--host or OPENCOLLAB_SWE_HOST": host,
        "--remote-root or OPENCOLLAB_SWE_REMOTE_ROOT": remote_root,
        "--image-repository or OPENCOLLAB_SWE_IMAGE_REPOSITORY": image_repository,
        "--model-name or OPENCOLLAB_SWE_MODEL_NAME": model_name,
        "--llm-model or OPENCOLLAB_SWE_LLM_MODEL": llm_model,
        "--proxy-env-file or OPENCOLLAB_PROXY_ENV_FILE": proxy_env_file,
        "--remote-proxy-base-url or OPENCOLLAB_REMOTE_PROXY_BASE_URL": remote_proxy_base_url,
        "--local-proxy-base-url or OPENCOLLAB_LOCAL_PROXY_BASE_URL": local_proxy_base_url,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise ValueError("missing required runtime configuration: " + ", ".join(missing))
    if workflow == "openhands-external" and not openhands_command:
        raise ValueError(
            "openhands-external requires --openhands-command or a solver entrypoint "
            "that supplies one"
        )
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
        model_name=model_name,
        llm_model=llm_model,
        context_window=getattr(args, "context_window", None),
        temperature=getattr(args, "temperature", None),
        top_p=getattr(args, "top_p", None),
        max_output_tokens=getattr(args, "max_output_tokens", None),
        session_prefix=session_prefix,
        host=host,
        ssh_command=args.ssh_command,
        remote_root=remote_root,
        image_repository=image_repository,
        workflow=workflow,
        workflow_env=normalize_workflow_env(getattr(args, "workflow_env", ())),
        openhands_command=openhands_command,
        openhands_empty_patch_rejections=max(
            0, getattr(args, "openhands_empty_patch_rejections", 2)
        ),
        max_empty_patch_retries=min(
            1, max(0, getattr(args, "max_empty_patch_retries", 1))
        ),
        remote_proxy_base_url=remote_proxy_base_url,
        local_proxy_base_url=local_proxy_base_url,
        proxy_env_file=Path(proxy_env_file),
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
    return solver_git_snapshot_valid(value)


SINGLE_TASK_COUNT_FIELDS = (
    "tasks",
    "generation_done",
    "empty_patch",
    "eval_done",
    "eval_attempts",
    "eval_retry_tasks",
    "resolved",
    "unresolved",
    "technical_failed",
)


def _expected_summary_identity(
    config: ParallelConfig, expected_index: int
) -> dict[str, Any]:
    expected = {
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
        expected["openhands_empty_patch_rejections"] = getattr(
            config, "openhands_empty_patch_rejections", 2
        )
        expected["openhands_command_sha256"] = _openhands_command_sha256(
            getattr(config, "openhands_command", "")
        )
    return expected


def _summary_runtime_identity_reasons(
    summary: dict[str, Any], config: ParallelConfig, expected_index: int
) -> list[str]:
    reasons = [
        f"summary_identity_mismatch:{key}"
        for key, expected in _expected_summary_identity(config, expected_index).items()
        if summary.get(key) != expected
    ]
    expected_workflow_env = {
        key: value
        for item in config.workflow_env
        for key, _, value in [item.partition("=")]
    }
    if summary.get("workflow_env") != expected_workflow_env:
        reasons.append("summary_identity_mismatch:workflow_env")
    return reasons


def _strict_success_row_reasons(
    row: Any, config: ParallelConfig, expected_index: int
) -> list[str]:
    if not isinstance(row, dict):
        return ["invalid_task_row"]
    if _eval_integrity.strict_index(row.get("index")) != expected_index:
        return ["task_row_index_mismatch"]
    task = str(row.get("task") or "").strip()
    if not task:
        return ["missing_task_identity"]
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    generation_status = generation.get("status")
    expected_eval = "skipped_empty_patch" if generation_status == "empty_patch" else "eval_done"
    reasons = list(_eval_integrity.attempt_integrity(row, task).reasons)
    if generation_status not in {"generation_done", "empty_patch"}:
        reasons.append("unexpected_terminal_generation_status")
    if evaluation.get("status") != expected_eval:
        reasons.append("unexpected_terminal_eval_status")
    if not current_generation_summary_proof_valid(generation):
        reasons.append("missing_trusted_generation_proof")
    if generation_status == "generation_done":
        if metric_submission_integrity(generation) != SUBMISSION_INTEGRITY_PROVEN:
            reasons.append("submission_integrity_unproven")
        if not _eval_integrity.attempt_integrity(row, task).direct_execution_proven:
            reasons.append("missing_direct_execution_proof")
    elif generation_status == "empty_patch" and not _eval_integrity.declared_empty_patch(row):
        reasons.append("empty_patch_integrity_unproven")
    return list(dict.fromkeys(reasons))


def single_task_summary_validation_reasons(
    summary: dict[str, Any], config: ParallelConfig, expected_index: int
) -> tuple[str, ...]:
    """Validate fresh and reusable single-task summaries against one contract."""
    reasons: list[str] = []
    if summary.get("schema") != "opencollab.swe_g11_prolite_runner.v1":
        reasons.append("invalid_summary_schema")
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    normalized: dict[str, int] = {}
    for count_name in SINGLE_TASK_COUNT_FIELDS:
        value = counts.get(count_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"invalid_count:{count_name}")
        else:
            normalized[count_name] = value
    if len(normalized) != len(SINGLE_TASK_COUNT_FIELDS):
        return tuple(reasons)

    rows = summary.get("rows") if isinstance(summary.get("rows"), list) else []
    status = str(summary.get("status") or "")
    if status in {"preflight_failed", "invalid_config"}:
        if (
            normalized["tasks"] != 0
            or rows
            or normalized["technical_failed"] != 1
            or any(normalized[count_name] for count_name in SINGLE_TASK_COUNT_FIELDS[1:-1])
        ):
            reasons.append("preflight_summary_count_conflict")
        return tuple(reasons)

    if status not in {"done", "done_with_technical_failures"}:
        reasons.append("nonterminal_runner_status")
        return tuple(reasons)
    reasons.extend(_summary_runtime_identity_reasons(summary, config, expected_index))
    if normalized["tasks"] != 1 or len(rows) != 1:
        reasons.append("terminal_summary_census_conflict")
        return tuple(dict.fromkeys(reasons))

    if status == "done":
        if (
            normalized["technical_failed"] != 0
            or normalized["generation_done"] + normalized["empty_patch"] != 1
            or normalized["eval_done"] + normalized["empty_patch"] != 1
            or normalized["resolved"] + normalized["unresolved"]
            != normalized["eval_done"]
        ):
            reasons.append("done_summary_count_conflict")
        reasons.extend(_strict_success_row_reasons(rows[0], config, expected_index))
        return tuple(dict.fromkeys(reasons))

    row = rows[0] if isinstance(rows[0], dict) else {}
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    evaluation_summary = (
        evaluation.get("summary") if isinstance(evaluation.get("summary"), dict) else {}
    )
    task = str(row.get("task") or "").strip()
    if (
        normalized["technical_failed"] != 1
        or normalized["resolved"] != 0
        or normalized["unresolved"] != 0
        or normalized["empty_patch"] != 0
        or normalized["eval_done"] != 0
        or normalized["generation_done"]
        != int(generation.get("status") == "generation_done")
    ):
        reasons.append("technical_summary_count_conflict")
    if _eval_integrity.strict_index(row.get("index")) != expected_index:
        reasons.append("task_row_index_mismatch")
    if not task:
        reasons.append("missing_task_identity")
    else:
        if str(generation.get("task") or "") != task:
            reasons.append("generation_task_mismatch")
        if str(evaluation.get("task") or "") != task:
            reasons.append("evaluation_task_mismatch")
        if evaluation.get("status") == "eval_done":
            reasons.append("technical_eval_status_conflict")
        if evaluation_summary.get("resolved") is True:
            reasons.append("technical_resolved_conflict")
        if generation.get("status") == "generation_done":
            identity = _eval_integrity.attempt_integrity(row, task)
            reasons.extend(identity.reasons)
            if metric_submission_integrity(generation) != SUBMISSION_INTEGRITY_PROVEN:
                reasons.append("submission_integrity_unproven")
            if not current_generation_summary_proof_valid(generation):
                reasons.append("missing_trusted_generation_proof")
        elif not str(generation.get("status") or ""):
            reasons.append("missing_generation_status")
    return tuple(dict.fromkeys(reasons))


def report_is_reusable(
    summary: dict[str, Any], config: ParallelConfig, expected_index: int
) -> bool:
    return bool(
        summary.get("status") == "done"
        and not single_task_summary_validation_reasons(summary, config, expected_index)
    )


def normalize_legacy_empty_patch_summary(
    summary: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Compatibility shim that leaves unverifiable legacy evidence unchanged."""
    return summary, False


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
