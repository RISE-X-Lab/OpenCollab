"""Summarize SWE-bench generation token and cost ledgers."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

WORKFLOW_RE = re.compile(
    r"workflow: tokens=(\d+) steps=(\d+) duration=(\d+)s error=([^\n]+)"
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _usage_from_record(record: dict[str, Any], model_filter: str | None) -> dict[str, Any] | None:
    if record.get("schema") == "opencollab.api_usage.v1":
        model = str(record.get("model") or "")
        if model_filter and model_filter not in model:
            return None
        usage = record.get("usage")
        return usage if isinstance(usage, dict) else None

    if record.get("type") == "llm_call":
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        model = str(payload.get("model") or "")
        if model_filter and model_filter not in model:
            return None
        usage = payload.get("usage")
        return usage if isinstance(usage, dict) else None

    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp > 0 else None


def _empty_api_totals() -> dict[str, Any]:
    return {
        "files": 0,
        "calls": 0,
        "input_tokens": 0,
        "uncached_input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "costed_calls": 0,
        "missing_cost_calls": 0,
        "estimated_calls": 0,
        "status_non_success": 0,
    }


def collect_api_usage(paths: Iterable[Path], model_filter: str | None = None) -> dict[str, Any]:
    totals = _empty_api_totals()
    groups: dict[tuple[str, Any], dict[str, Any]] = {}
    seen: set[Path] = set()
    for root in paths:
        root = Path(root)
        candidates = [root] if root.name == "api_usage.jsonl" else root.rglob("api_usage.jsonl")
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            file_calls = 0
            for record in _iter_jsonl(path):
                usage = _usage_from_record(record, model_filter)
                if usage is None:
                    continue
                file_calls += 1
                totals["calls"] += 1
                input_tokens = _int(usage.get("input_tokens"))
                cached_tokens = _int(
                    usage.get("cached_input_tokens", usage.get("cache_read_tokens"))
                )
                cache_creation = _int(usage.get("cache_creation_tokens"))
                uncached = _int(usage.get("uncached_input_tokens"))
                if uncached == 0 and input_tokens:
                    uncached = max(input_tokens - cached_tokens - cache_creation, 0)
                output_tokens = _int(usage.get("output_tokens"))
                total_tokens = _int(usage.get("total_tokens"))
                if total_tokens == 0:
                    total_tokens = input_tokens + output_tokens

                totals["input_tokens"] += input_tokens
                totals["uncached_input_tokens"] += uncached
                totals["cached_input_tokens"] += cached_tokens
                totals["cache_creation_tokens"] += cache_creation
                totals["output_tokens"] += output_tokens
                totals["total_tokens"] += total_tokens
                if "cost_usd" in usage and usage.get("cost_usd") is not None:
                    totals["cost_usd"] += _float(usage.get("cost_usd"))
                    totals["costed_calls"] += 1
                else:
                    totals["missing_cost_calls"] += 1
                if usage.get("estimated"):
                    totals["estimated_calls"] += 1
                if record.get("status") not in (None, "success"):
                    totals["status_non_success"] += 1
                group_key = (str(path), record.get("pid"))
                group = groups.setdefault(
                    group_key,
                    {
                        "api_usage_path": str(path),
                        "pid": record.get("pid"),
                        "cwd": record.get("cwd"),
                        "argv0": record.get("argv0"),
                        "run_id": record.get("run_id"),
                        "label": record.get("label"),
                        "first_timestamp": None,
                        "last_timestamp": None,
                        "calls": 0,
                        "input_tokens": 0,
                        "uncached_input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_creation_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0,
                        "costed_calls": 0,
                        "missing_cost_calls": 0,
                    },
                )
                group["calls"] += 1
                group["input_tokens"] += input_tokens
                group["uncached_input_tokens"] += uncached
                group["cached_input_tokens"] += cached_tokens
                group["cache_creation_tokens"] += cache_creation
                group["output_tokens"] += output_tokens
                group["total_tokens"] += total_tokens
                timestamp = _timestamp(record.get("timestamp"))
                if timestamp is not None:
                    first_timestamp = group.get("first_timestamp")
                    last_timestamp = group.get("last_timestamp")
                    group["first_timestamp"] = (
                        timestamp if first_timestamp is None else min(float(first_timestamp), timestamp)
                    )
                    group["last_timestamp"] = (
                        timestamp if last_timestamp is None else max(float(last_timestamp), timestamp)
                    )
                if "cost_usd" in usage and usage.get("cost_usd") is not None:
                    group["cost_usd"] += _float(usage.get("cost_usd"))
                    group["costed_calls"] += 1
                else:
                    group["missing_cost_calls"] += 1
            if file_calls:
                totals["files"] += 1
    totals["cost_usd"] = round(totals["cost_usd"], 8)
    totals["cost_usd_complete"] = totals["calls"] > 0 and totals["missing_cost_calls"] == 0
    group_values = []
    for group in groups.values():
        group["cost_usd"] = round(group["cost_usd"], 8)
        group["cost_usd_complete"] = group["missing_cost_calls"] == 0
        group_values.append(group)
    totals["groups"] = sorted(
        group_values,
        key=lambda group: (str(group["api_usage_path"]), str(group.get("pid"))),
    )
    return totals


def collect_workflow_usage(paths: Iterable[Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in paths:
        root = Path(root)
        candidates = [root] if root.name.endswith(".outer.log") else root.rglob("*.outer.log")
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in WORKFLOW_RE.finditer(text):
                records.append(
                    {
                        "path": str(path),
                        "tokens": int(match.group(1)),
                        "steps": int(match.group(2)),
                        "duration_s": int(match.group(3)),
                        "error": match.group(4).strip(),
                        "mtime": path.stat().st_mtime,
                    }
                )
    return {
        "outer_logs": len({record["path"] for record in records}),
        "attempts": len(records),
        "total_tokens": sum(record["tokens"] for record in records),
        "steps": sum(record["steps"] for record in records),
        "duration_s": sum(record["duration_s"] for record in records),
        "records": records,
    }


def build_summary(
    run_dirs: Iterable[Path],
    *,
    model_filter: str | None = None,
    usd_cny: float | None = None,
) -> dict[str, Any]:
    roots = [Path(path) for path in run_dirs]
    api_usage = collect_api_usage(roots, model_filter=model_filter)
    workflow = collect_workflow_usage(roots)
    source = "api_usage" if api_usage["calls"] else "workflow_log"
    total_tokens = api_usage["total_tokens"] if api_usage["calls"] else workflow["total_tokens"]
    cost_usd = api_usage["cost_usd"] if api_usage.get("cost_usd_complete") else None
    billable: dict[str, Any] = {
        "source": source,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }
    if api_usage["calls"] and not api_usage.get("cost_usd_complete"):
        billable["partial_cost_usd"] = api_usage["cost_usd"]
        billable["missing_cost_calls"] = api_usage["missing_cost_calls"]
    if usd_cny is not None and cost_usd is not None:
        billable["usd_cny"] = usd_cny
        billable["cost_cny"] = round(cost_usd * usd_cny, 6)

    workflow_delta = api_usage["total_tokens"] - workflow["total_tokens"]
    consistency = {
        "api_minus_workflow_tokens": workflow_delta,
        "api_covers_workflow": bool(api_usage["calls"])
        and api_usage["total_tokens"] >= workflow["total_tokens"],
    }
    if workflow_delta > 0:
        consistency["note"] = (
            "api_usage includes successful model calls that did not form a complete workflow record"
        )
    elif workflow_delta < 0:
        consistency["note"] = (
            "workflow logs include tokens without matching api_usage cost records"
        )

    return {
        "schema": "opencollab.swe_token_cost_summary.v1",
        "generated_at": _now(),
        "run_dirs": [str(path) for path in roots],
        "model_filter": model_filter,
        "api_usage": api_usage,
        "workflow": workflow,
        "billable": billable,
        "consistency": consistency,
    }


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": summary.get("schema"),
        "generated_at": summary.get("generated_at"),
        "run_dirs": summary.get("run_dirs"),
        "model_filter": summary.get("model_filter"),
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


def to_markdown(summary: dict[str, Any]) -> str:
    billable = summary.get("billable") if isinstance(summary.get("billable"), dict) else {}
    api = summary.get("api_usage") if isinstance(summary.get("api_usage"), dict) else {}
    workflow = summary.get("workflow") if isinstance(summary.get("workflow"), dict) else {}
    consistency = summary.get("consistency") if isinstance(summary.get("consistency"), dict) else {}
    lines = [
        "# SWE Token Cost Summary",
        "",
        f"- generated_at: `{summary.get('generated_at')}`",
        f"- model_filter: `{summary.get('model_filter')}`",
        f"- run_dirs: `{len(summary.get('run_dirs') or [])}`",
        f"- billable_source: `{billable.get('source')}`",
        f"- billable_total_tokens: `{billable.get('total_tokens')}`",
        f"- billable_cost_usd: `{billable.get('cost_usd')}`",
    ]
    if "partial_cost_usd" in billable:
        lines.append(f"- partial_cost_usd: `{billable.get('partial_cost_usd')}`")
        lines.append(f"- missing_cost_calls: `{billable.get('missing_cost_calls')}`")
    if "cost_cny" in billable:
        lines.extend(
            [
                f"- usd_cny: `{billable.get('usd_cny')}`",
                f"- billable_cost_cny: `{billable.get('cost_cny')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## API Usage",
            "",
            f"- files: `{api.get('files')}`",
            f"- calls: `{api.get('calls')}`",
            f"- input_tokens: `{api.get('input_tokens')}`",
            f"- uncached_input_tokens: `{api.get('uncached_input_tokens')}`",
            f"- cached_input_tokens: `{api.get('cached_input_tokens')}`",
            f"- output_tokens: `{api.get('output_tokens')}`",
            f"- total_tokens: `{api.get('total_tokens')}`",
            f"- cost_usd: `{api.get('cost_usd')}`",
            f"- cost_usd_complete: `{api.get('cost_usd_complete')}`",
            f"- missing_cost_calls: `{api.get('missing_cost_calls')}`",
            "",
            "## Workflow Cross Check",
            "",
            f"- outer_logs: `{workflow.get('outer_logs')}`",
            f"- attempts: `{workflow.get('attempts')}`",
            f"- total_tokens: `{workflow.get('total_tokens')}`",
            f"- api_minus_workflow_tokens: `{consistency.get('api_minus_workflow_tokens')}`",
        ]
    )
    if consistency.get("note"):
        lines.append(f"- note: `{consistency.get('note')}`")
    return "\n".join(lines) + "\n"
