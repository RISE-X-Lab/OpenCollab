"""Report aggregation and persistence for the G1.1 parallel runner."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts._swe_g11_config import (
        REPO,
        ParallelConfig,
        _openhands_command_sha256,
        range_label,
    )
except ModuleNotFoundError:  # Direct execution adds ``scripts`` rather than the repo root.
    from _swe_g11_config import (  # type: ignore[no-redef]
        REPO,
        ParallelConfig,
        _openhands_command_sha256,
        range_label,
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _compact_token_summary(
    summary: dict[str, Any], config: ParallelConfig
) -> dict[str, Any]:
    return {
        "summary_json": str(config.output_dir / "parallel_token_cost_summary.json"),
        "summary_markdown": str(config.output_dir / "parallel_token_cost_summary.md"),
        "billable": summary.get("billable"),
        "api_usage": {
            key: value
            for key, value in (summary.get("api_usage") or {}).items()
            if key
            in {
                "files",
                "calls",
                "input_tokens",
                "uncached_input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "costed_calls",
                "missing_cost_calls",
                "cost_usd_complete",
                "estimated_calls",
                "status_non_success",
            }
        },
        "workflow": {
            key: value
            for key, value in (summary.get("workflow") or {}).items()
            if key in {"outer_logs", "attempts", "total_tokens", "steps", "duration_s"}
        },
        "consistency": summary.get("consistency"),
    }


def build_token_summary(config: ParallelConfig) -> dict[str, Any]:
    json_path = config.output_dir / "parallel_token_cost_summary.json"
    md_path = config.output_dir / "parallel_token_cost_summary.md"
    cmd = [
        sys.executable,
        "scripts/swe_token_cost_summary.py",
        "--remote-host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--run-dir",
        config.remote_base,
        "--json-output",
        str(json_path),
        "--markdown-output",
        str(md_path),
        "--compact",
    ]
    if config.usd_cny is not None:
        cmd.extend(["--usd-cny", str(config.usd_cny)])
    proc = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
    (config.output_dir / "parallel_token_cost_summary.stdout.log").write_text(
        proc.stdout, encoding="utf-8", errors="replace"
    )
    (config.output_dir / "parallel_token_cost_summary.stderr.log").write_text(
        proc.stderr, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "summary_json": str(json_path),
            "summary_markdown": str(md_path),
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-2000:],
        }
    return _compact_token_summary(load_json(json_path), config)


def build_eval_fact_report(config: ParallelConfig) -> dict[str, Any]:
    token_json = config.output_dir / "parallel_token_cost_summary.json"
    output_json = config.output_dir / "final_eval_layer_report.json"
    output_md = config.output_dir / "final_eval_layer_report.md"
    cmd = [
        sys.executable,
        "scripts/swe_eval_layer_report.py",
        "--token-cost-json",
        str(token_json),
        "--json-output",
        str(output_json),
        "--markdown-output",
        str(output_md),
        "--report-json",
        str(config.output_dir / "parallel_summary.json"),
    ]
    if config.usd_cny is not None:
        cmd.extend(["--usd-cny", str(config.usd_cny)])
    proc = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
    (config.output_dir / "final_eval_layer_report.stdout.log").write_text(
        proc.stdout, encoding="utf-8", errors="replace"
    )
    (config.output_dir / "final_eval_layer_report.stderr.log").write_text(
        proc.stderr, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "summary_json": str(output_json),
            "summary_markdown": str(output_md),
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-2000:],
        }
    report = load_json(output_json)
    return {
        "status": "done" if report else "missing_report",
        "summary_json": str(output_json),
        "summary_markdown": str(output_md),
        "counts": report.get("counts") if isinstance(report, dict) else {},
    }


def aggregate(
    config: ParallelConfig,
    results: list[dict[str, Any]],
    running: list[int] | None = None,
    token_cost: dict[str, Any] | None = None,
    fact_report: dict[str, Any] | None = None,
    scheduler: dict[str, Any] | None = None,
    remote_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(results, key=lambda item: item["index"])
    counts = {
        "tasks": sum(int(item.get("tasks") or 0) for item in ordered),
        "generation_done": sum(
            int(item.get("generation_done") or 0) for item in ordered
        ),
        "empty_patch": sum(int(item.get("empty_patch") or 0) for item in ordered),
        "eval_done": sum(int(item.get("eval_done") or 0) for item in ordered),
        "eval_attempts": sum(int(item.get("eval_attempts") or 0) for item in ordered),
        "eval_retry_tasks": sum(
            int(item.get("eval_retry_tasks") or 0) for item in ordered
        ),
        "resolved": sum(int(item.get("resolved") or 0) for item in ordered),
        "unresolved": sum(int(item.get("unresolved") or 0) for item in ordered),
        "technical_failed": sum(
            int(item.get("technical_failed") or 0) for item in ordered
        ),
    }
    status = (
        "done"
        if len(ordered) == len(config.indices)
        and all(item.get("completed") for item in ordered)
        else "running"
    )
    if status == "done" and counts["technical_failed"] > 0:
        status = "done_with_technical_failures"
    elif status == "done" and any(
        item.get("returncode") not in (0, 1) for item in ordered
    ):
        status = "done_with_runner_failures"
    summary = {
        "schema": "opencollab.swe_parallel_runner.v2",
        "status": status,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "range": range_label(config.indices),
        "indices": list(config.indices),
        "run_id": config.run_id,
        "max_workers": config.max_workers,
        "remote_base": config.remote_base,
        "remote_runtime_repo": config.remote_runtime_repo,
        "output_dir": str(config.output_dir),
        "model_name": config.model_name,
        "llm_model": config.llm_model,
        "context_window": config.context_window,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_output_tokens,
        "workflow": config.workflow,
        "workflow_env": list(config.workflow_env),
        "openhands_command_sha256": _openhands_command_sha256(
            getattr(config, "openhands_command", "")
        ),
        "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
        "max_empty_patch_retries": config.max_empty_patch_retries,
        "budget": config.budget,
        "max_steps": config.max_steps,
        "counts": counts,
        "running": running or [],
        "results": ordered,
    }
    if token_cost:
        summary["token_cost"] = token_cost
    if fact_report:
        summary["fact_report"] = fact_report
    if scheduler:
        summary["scheduler"] = scheduler
    if remote_health:
        summary["remote_health"] = remote_health
    return summary


def compact_progress(summary: dict[str, Any]) -> dict[str, Any]:
    value = {
        "status": summary.get("status"),
        "generated_at": summary.get("generated_at"),
        "range": summary.get("range"),
        "run_id": summary.get("run_id"),
        "max_workers": summary.get("max_workers"),
        "counts": summary.get("counts"),
        "running": summary.get("running"),
        "results": [
            {
                "index": item.get("index"),
                "returncode": item.get("returncode"),
                "runner_status": item.get("runner_status"),
                "generation_done": item.get("generation_done"),
                "eval_done": item.get("eval_done"),
                "resolved": item.get("resolved"),
                "unresolved": item.get("unresolved"),
                "technical_failed": item.get("technical_failed"),
                "completed": item.get("completed"),
                "attempts": item.get("attempts"),
                "reused_existing_report": item.get("reused_existing_report"),
            }
            for item in summary.get("results", [])
        ],
    }
    if "token_cost" in summary:
        value["token_cost"] = summary["token_cost"]
    if "fact_report" in summary:
        value["fact_report"] = summary["fact_report"]
    if "scheduler" in summary:
        value["scheduler"] = summary["scheduler"]
    if "remote_health" in summary:
        value["remote_health"] = summary["remote_health"]
    return value


def write_markdown(config: ParallelConfig, summary: dict[str, Any]) -> None:
    lines = [
        f"# SWE Pro-Lite {summary['range']} Parallel Report",
        "",
        f"- status: `{summary['status']}`",
        f"- run_id: `{summary['run_id']}`",
        f"- max_workers: `{summary['max_workers']}`",
        f"- remote_base: `{summary['remote_base']}`",
        f"- output_dir: `{summary['output_dir']}`",
        f"- model_name: `{summary['model_name']}`",
        f"- llm_model: `{summary['llm_model']}`",
        f"- context_window: `{summary['context_window']}`",
        f"- temperature: `{summary['temperature']}`",
        f"- top_p: `{summary['top_p']}`",
        f"- max_output_tokens: `{summary['max_output_tokens']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- workflow_env: `{summary['workflow_env']}`",
        f"- budget: `{summary['budget']}`",
        f"- max_steps: `{summary['max_steps']}`",
        (
            "- openhands_empty_patch_rejections: "
            f"`{summary['openhands_empty_patch_rejections']}`"
        ),
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- empty_patch: `{summary['counts'].get('empty_patch', 0)}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
        f"- eval_attempts: `{summary['counts']['eval_attempts']}`",
        f"- eval_retry_tasks: `{summary['counts']['eval_retry_tasks']}`",
        f"- resolved: `{summary['counts']['resolved']}`",
        f"- unresolved: `{summary['counts']['unresolved']}`",
        f"- technical_failed: `{summary['counts']['technical_failed']}`",
    ]
    scheduler = (
        summary.get("scheduler") if isinstance(summary.get("scheduler"), dict) else {}
    )
    if scheduler:
        lines.extend(
            [
                f"- adaptive_concurrency: `{scheduler.get('adaptive_concurrency')}`",
                f"- current_workers: `{scheduler.get('current_workers')}`",
                f"- scheduler_events: `{len(scheduler.get('events') or [])}`",
            ]
        )
    remote_health = (
        summary.get("remote_health")
        if isinstance(summary.get("remote_health"), dict)
        else {}
    )
    if remote_health:
        lines.append(f"- remote_health: `{remote_health.get('status')}`")
    token_cost = (
        summary.get("token_cost") if isinstance(summary.get("token_cost"), dict) else {}
    )
    billable = (
        token_cost.get("billable")
        if isinstance(token_cost.get("billable"), dict)
        else {}
    )
    if billable:
        lines.extend(
            [
                f"- billable_total_tokens: `{billable.get('total_tokens')}`",
                f"- billable_cost_usd: `{billable.get('cost_usd')}`",
                f"- token_cost_summary: `{token_cost.get('summary_json')}`",
            ]
        )
        if "cost_cny" in billable:
            lines.append(f"- billable_cost_cny: `{billable.get('cost_cny')}`")
    fact_report = (
        summary.get("fact_report")
        if isinstance(summary.get("fact_report"), dict)
        else {}
    )
    if fact_report:
        lines.extend(
            [
                f"- final_fact_report_json: `{fact_report.get('summary_json')}`",
                f"- final_fact_report_markdown: `{fact_report.get('summary_markdown')}`",
            ]
        )
    lines.extend(
        [
            "",
            "| idx | rc | runner | gen | eval | resolved | unresolved | technical | report |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    row_template = (
        "| {idx} | `{rc}` | `{runner}` | `{gen}` | `{eval}` | `{resolved}` | "
        "`{unresolved}` | `{technical}` | `{report}` |"
    )
    for item in summary["results"]:
        lines.append(
            row_template.format(
                idx=item["index"],
                rc=item.get("returncode"),
                runner=item.get("runner_status"),
                gen=item.get("generation_done"),
                eval=item.get("eval_done"),
                resolved=item.get("resolved"),
                unresolved=item.get("unresolved"),
                technical=item.get("technical_failed"),
                report=item.get("json_report"),
            )
        )
    (config.output_dir / "parallel_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def save_progress(
    config: ParallelConfig,
    results: list[dict[str, Any]],
    running: list[int] | None = None,
    token_cost: dict[str, Any] | None = None,
    fact_report: dict[str, Any] | None = None,
    scheduler: dict[str, Any] | None = None,
    remote_health: dict[str, Any] | None = None,
) -> None:
    summary = aggregate(
        config,
        results,
        running,
        token_cost=token_cost,
        fact_report=fact_report,
        scheduler=scheduler,
        remote_health=remote_health,
    )
    write_json(config.output_dir / "parallel_summary.json", summary)
    write_markdown(config, summary)
