#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "opencollab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "opencollab"))

from opencollab.harness.token_cost import WORKFLOW_RE  # noqa: E402

try:
    from scripts import _swe_eval_layer_integrity as _integrity  # noqa: E402
    from scripts import _swe_report_io as _report_io  # noqa: E402
except ModuleNotFoundError as exc:  # Direct execution adds ``scripts`` to sys.path.
    if exc.name != "scripts":
        raise
    import _swe_eval_layer_integrity as _integrity  # type: ignore[no-redef]
    import _swe_report_io as _report_io  # type: ignore[no-redef]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def _load_json(path: Path) -> dict[str, Any]:
    return _report_io.load_json(path)


def _iter_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(report.get("rows"), list):
        rows.extend(row for row in report["rows"] if isinstance(row, dict))
    for result in report.get("results") or []:
        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            rows.extend(row for row in result["rows"] if isinstance(row, dict))
    return rows


def _patch_sha(row: dict[str, Any]) -> str:
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    summary = evaluation.get("summary") if isinstance(evaluation.get("summary"), dict) else {}
    generation_sha = str(generation.get("patch_sha256") or "")
    evaluation_sha = str(summary.get("patch_sha256") or "")
    return generation_sha or evaluation_sha


def _task_key(row: dict[str, Any]) -> str:
    return str(row.get("task") or row.get("instance_id") or "")


def _task_index(row: dict[str, Any]) -> int | None:
    return _integrity.strict_index(row.get("index"))


def _eval_state(row: dict[str, Any]) -> dict[str, Any]:
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    summary = evaluation.get("summary") if isinstance(evaluation.get("summary"), dict) else {}
    status = str(evaluation.get("status") or "")
    resolved = summary.get("resolved")
    integrity = _integrity.attempt_integrity(row, _task_key(row))
    eval_success = status == "eval_done" and isinstance(resolved, bool) and not integrity.reasons
    technical_reasons = [
        str(reason) for reason in summary.get("technical_reasons") or [] if str(reason)
    ]
    technical_reasons.extend(
        reason for reason in integrity.reasons if reason not in technical_reasons
    )
    allowed_statuses = {"eval_done", "skipped_empty_patch", "would_eval"}
    if status not in allowed_statuses:
        status_reason = (
            "missing_eval_status" if not status else f"unexpected_eval_status:{status}"
        )
        if status_reason not in technical_reasons:
            technical_reasons.append(status_reason)
    technical_failed = bool(integrity.reasons or status not in allowed_statuses)
    return {
        "eval_status": "technical_identity_failure" if integrity.reasons else status,
        "eval_success": eval_success,
        "eval_pending": status == "would_eval" and not integrity.reasons,
        "resolved": resolved if eval_success else None,
        "technical_failed": technical_failed,
        "report_path": evaluation.get("report_path") or summary.get("report_path"),
        "technical_reasons": technical_reasons,
        "identity_reasons": list(integrity.reasons),
        "record_id": integrity.record_id,
        "direct_execution_proven": integrity.direct_execution_proven,
    }


def _generation_state(row: dict[str, Any]) -> dict[str, Any]:
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    integrity = _integrity.attempt_integrity(row, _task_key(row))
    return {
        "generation_status": generation.get("status"),
        "patch_len": generation.get("patch_len"),
        "workflow_status": generation.get("workflow_status"),
        "artifact_workflow": generation.get("artifact_workflow"),
        "artifact_model_name": generation.get("artifact_model_name"),
        "artifact_identity_status": generation.get("artifact_identity_status"),
        "llm_model": generation.get("llm_model"),
        "llm_provider": generation.get("llm_provider"),
        "context_window": generation.get("context_window"),
        "temperature": generation.get("temperature"),
        "top_p": generation.get("top_p"),
        "max_output_tokens": generation.get("max_output_tokens"),
        "patch_sha256": integrity.patch_sha256 or _patch_sha(row),
        "record_id": integrity.record_id,
        "direct_execution_proven": integrity.direct_execution_proven,
        "generation_log": generation.get("log"),
    }


def _is_declared_empty_patch(attempt: dict[str, Any]) -> bool:
    return _integrity.declared_empty_patch(attempt.get("row") or {})


def _is_pending_dry_run(attempt: dict[str, Any]) -> bool:
    return bool(
        attempt.get("generation_status") == "would_generate"
        and attempt.get("eval_status") == "would_eval"
    )


def _load_workflow_records(token_cost: dict[str, Any]) -> list[dict[str, Any]]:
    workflow = token_cost.get("workflow") if isinstance(token_cost.get("workflow"), dict) else {}
    return [record for record in workflow.get("records") or [] if isinstance(record, dict)]


def _read_workflow_records_from_log(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    local = Path(path)
    try:
        text = _report_io.read_text(local, max_bytes=_report_io.MAX_LOG_BYTES)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    return [
        {
            "path": str(local),
            "tokens": int(match.group(1)),
            "steps": int(match.group(2)),
            "duration_s": int(match.group(3)),
            "error": match.group(4).strip(),
        }
        for match in WORKFLOW_RE.finditer(text)
    ]


def _task_workflow_records(row: dict[str, Any], token_cost: dict[str, Any]) -> list[dict[str, Any]]:
    log_path = _generation_state(row).get("generation_log")
    records = [
        record
        for record in _load_workflow_records(token_cost)
        if log_path and record.get("path") == log_path
    ]
    if records:
        return records
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    if generation.get("tokens_used") is not None:
        return [
            {
                "path": log_path,
                "tokens": generation.get("tokens_used"),
                "steps": generation.get("steps"),
                "duration_s": generation.get("duration_s"),
                "error": None,
            }
        ]
    return _read_workflow_records_from_log(log_path)


def _api_groups(token_cost: dict[str, Any]) -> list[dict[str, Any]]:
    api = token_cost.get("api_usage") if isinstance(token_cost.get("api_usage"), dict) else {}
    return [group for group in api.get("groups") or [] if isinstance(group, dict)]


def _strip_api_usage_suffix(path: str) -> str:
    normalized = str(path or "")
    for suffix in (
        "/_runtime/repo/.opencollab/logs/api_usage.jsonl",
        "/api_usage.jsonl",
    ):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _path_match_score(record: dict[str, Any], group: dict[str, Any]) -> int:
    record_path = str(record.get("path") or "")
    group_root = _strip_api_usage_suffix(str(group.get("api_usage_path") or ""))
    if not record_path or not group_root:
        return 0
    if record_path.startswith(group_root + "/"):
        return 40
    parent = str(Path(record_path).parent.parent) if "/generation_logs/" in record_path else ""
    if parent and parent.startswith(group_root + "/"):
        return 30
    task_match = re.search(r"/task_\d+(?:/|$)", group_root)
    if task_match and group_root[: task_match.end() - 1] in record_path:
        return 20
    return 0


def _time_match_score(record: dict[str, Any], group: dict[str, Any]) -> int:
    try:
        mtime = float(record.get("mtime") or 0)
        first_timestamp = float(group.get("first_timestamp") or 0)
        last_timestamp = float(group.get("last_timestamp") or 0)
    except (TypeError, ValueError):
        return 0
    if not mtime or not first_timestamp or not last_timestamp:
        return 0
    duration = max(float(record.get("duration_s") or 0), 0.0)
    if first_timestamp - 600 <= mtime <= last_timestamp + duration + 600:
        return 5
    return 0


def _candidate_score(record: dict[str, Any], group: dict[str, Any]) -> int:
    score = _path_match_score(record, group) + _time_match_score(record, group)
    try:
        steps = int(record.get("steps") or 0)
        calls = int(group.get("calls") or 0)
    except (TypeError, ValueError):
        steps = calls = 0
    if steps and calls == steps:
        score += 100
    return score


def _match_group(
    record: dict[str, Any],
    groups: list[dict[str, Any]],
    used: set[int],
) -> tuple[int | None, str]:
    record_tokens = int(record.get("tokens") or 0)
    candidates = [
        (index, _candidate_score(record, group))
        for index, group in enumerate(groups)
        if index not in used and int(group.get("total_tokens") or 0) == record_tokens
    ]
    if not candidates:
        return None, "no_api_usage_group"
    candidates.sort(key=lambda item: item[1], reverse=True)
    if candidates[0][1] <= 0:
        return None, "insufficient_api_usage_context"
    if len(candidates) > 1 and candidates[0][1] == candidates[1][1]:
        return None, "ambiguous_api_usage_group"
    return candidates[0][0], "matched_by_context"


def _record_key(record: dict[str, Any], row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("path"),
        record.get("tokens"),
        record.get("steps"),
        record.get("duration_s"),
    )


def _assign_costs(rows: list[dict[str, Any]], token_cost: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = _api_groups(token_cost)
    used: set[int] = set()
    assigned: dict[str, dict[str, Any]] = {}
    seen_records_by_task: dict[str, set[tuple[Any, ...]]] = {}
    seen_record_fingerprints_by_task: dict[str, set[tuple[Any, ...]]] = {}
    seen_path_token_values_by_task: dict[str, set[Any]] = {}
    for row in rows:
        task = _task_key(row)
        if not task:
            continue
        entry = assigned.setdefault(
            task,
            {
                "workflow_tokens": 0,
                "workflow_attempts": 0,
                "cost_usd": 0.0,
                "cost_usd_complete": True,
                "api_usage_groups": [],
                "cost_assignment_notes": [],
            },
        )
        workflow_records = _task_workflow_records(row, token_cost)
        if not workflow_records:
            log_path = _generation_state(row).get("generation_log")
            direct_groups = [
                (index, group)
                for index, group in enumerate(groups)
                if index not in used
                and (
                    _path_match_score({"path": log_path}, group) > 0
                    or task in Path(_strip_api_usage_suffix(str(group.get("api_usage_path") or ""))).parts
                )
            ]
            for match_index, group in direct_groups:
                used.add(match_index)
                entry["workflow_attempts"] += 1
                entry["workflow_tokens"] += int(group.get("total_tokens") or 0)
                entry["api_usage_groups"].append(
                    {
                        "api_usage_path": group.get("api_usage_path"),
                        "pid": group.get("pid"),
                        "total_tokens": group.get("total_tokens"),
                        "calls": group.get("calls"),
                        "cost_usd": group.get("cost_usd"),
                        "cost_usd_complete": group.get("cost_usd_complete"),
                        "match_reason": "matched_by_task_directory",
                    }
                )
                if group.get("cost_usd_complete"):
                    entry["cost_usd"] += float(group.get("cost_usd") or 0.0)
                else:
                    entry["cost_usd_complete"] = False
            if direct_groups:
                continue
        for record in workflow_records:
            fingerprint = (record.get("tokens"), record.get("steps"), record.get("duration_s"))
            seen_fingerprints = seen_record_fingerprints_by_task.setdefault(task, set())
            path_token_values = seen_path_token_values_by_task.setdefault(task, set())
            if not record.get("path") and (
                fingerprint in seen_fingerprints
                or record.get("tokens") in path_token_values
            ):
                continue
            record_key = _record_key(record, row)
            if record_key in seen_records_by_task.setdefault(task, set()):
                continue
            seen_records_by_task[task].add(record_key)
            seen_fingerprints.add(fingerprint)
            if record.get("path"):
                path_token_values.add(record.get("tokens"))
            entry["workflow_attempts"] += 1
            record_tokens = int(record.get("tokens") or 0)
            entry["workflow_tokens"] += record_tokens
            match_index, match_reason = _match_group(record, groups, used)
            if match_index is None:
                entry["cost_usd_complete"] = False
                entry["cost_assignment_notes"].append(
                    {
                        "reason": match_reason,
                        "record_path": record.get("path"),
                        "record_tokens": record_tokens,
                        "record_steps": record.get("steps"),
                    }
                )
                continue
            used.add(match_index)
            group = groups[match_index]
            entry["api_usage_groups"].append(
                {
                    "api_usage_path": group.get("api_usage_path"),
                    "pid": group.get("pid"),
                    "total_tokens": group.get("total_tokens"),
                    "calls": group.get("calls"),
                    "cost_usd": group.get("cost_usd"),
                    "cost_usd_complete": group.get("cost_usd_complete"),
                    "match_reason": match_reason,
                }
            )
            if group.get("cost_usd_complete"):
                entry["cost_usd"] += float(group.get("cost_usd") or 0.0)
            else:
                entry["cost_usd_complete"] = False
    for entry in assigned.values():
        matched_groups = entry["api_usage_groups"]
        complete = bool(entry["cost_usd_complete"] and matched_groups)
        entry["cost_usd"] = round(float(entry["cost_usd"]), 8) if complete else None
        entry["cost_usd_complete"] = complete
    return assigned


def _row_score(row: dict[str, Any], round_number: int) -> tuple[int, int]:
    evaluation = _eval_state(row)
    generation = _generation_state(row)
    if evaluation["eval_success"]:
        return (3, round_number)
    if generation["generation_status"] == "generation_done":
        return (2, round_number)
    return (1, round_number)


def _eval_attempt_count(row: dict[str, Any]) -> int:
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    attempts = evaluation.get("attempts")
    if isinstance(attempts, list) and attempts:
        return sum(_eval_result_executed(attempt) for attempt in attempts if isinstance(attempt, dict))
    if not _eval_result_executed(evaluation):
        return 0
    try:
        count = int(evaluation.get("attempt_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count or 1


def _eval_result_executed(result: dict[str, Any]) -> bool:
    if result.get("executed") is False:
        return False
    return str(result.get("status") or "") not in {
        "",
        "would_eval",
        "skipped_no_generation_patch",
        "skipped_empty_patch",
        "blocked_missing_eval_image",
    }


def _compact_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "round": attempt.get("round"),
        "source_report": attempt.get("source_report"),
        "index": attempt.get("index"),
        "task": attempt.get("task"),
        "generation_status": attempt.get("generation_status"),
        "eval_status": attempt.get("eval_status"),
        "eval_success": attempt.get("eval_success"),
        "eval_pending": attempt.get("eval_pending"),
        "resolved": attempt.get("resolved"),
        "technical_failed": attempt.get("technical_failed"),
        "technical_reasons": attempt.get("technical_reasons"),
        "record_id": attempt.get("record_id"),
        "patch_sha256": attempt.get("patch_sha256"),
        "direct_execution_proven": attempt.get("direct_execution_proven"),
        "report_path": attempt.get("report_path"),
        "eval_attempt_count": _eval_attempt_count(attempt.get("row") or {}),
    }


def build_report(
    report_paths: list[Path],
    *,
    token_cost_path: Path | None = None,
    expected_indices: tuple[int, ...] | list[int] | None = None,
    max_rounds: int = 2,
    max_eval_attempts: int = 2,
    allow_over_budget_evidence: bool = False,
    usd_cny: float | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    observed_attempts: list[dict[str, Any]] = []
    rounds_by_task: dict[str, int] = {}
    used_reports: list[str] = []
    task_issues: dict[str, list[str]] = {}
    issues_by_index: dict[int, list[str]] = {}
    global_census_issues: list[str] = []
    inferred_indices: list[int] = []
    seen_rows: set[tuple[str, int | None, str]] = set()
    ordered_report_paths = sorted(report_paths, key=lambda path: str(path))
    for source_order, path in enumerate(ordered_report_paths, start=1):
        report, load_error = _report_io.load_json_with_error(path)
        if load_error:
            global_census_issues.append(f"{load_error}:{path}")
            continue
        report_indices, report_issues, report_global_issues = _integrity.report_census(report)
        for index in report_indices:
            if index not in inferred_indices:
                inferred_indices.append(index)
        for index, reasons in report_issues.items():
            for reason in reasons:
                _integrity.append_issue(issues_by_index, index, reason)
        for reason in report_global_issues:
            if reason not in global_census_issues:
                global_census_issues.append(reason)
        for row in _iter_rows(report):
            task = _task_key(row)
            if not task:
                index = _task_index(row)
                if index is None:
                    if "missing_task_identity" not in global_census_issues:
                        global_census_issues.append("missing_task_identity")
                else:
                    _integrity.append_issue(issues_by_index, index, "missing_task_identity")
                continue
            index = _task_index(row)
            row_key = (str(path), index, task)
            if row_key in seen_rows:
                _integrity.append_issue(task_issues, task, "duplicate_task_row")
                if index is not None:
                    _integrity.append_issue(issues_by_index, index, "duplicate_task_row")
            seen_rows.add(row_key)
            round_number = rounds_by_task.get(task, 0) + 1
            rounds_by_task[task] = round_number
            observed = {
                "round": round_number,
                "source_order": source_order,
                "source_report": str(path),
                "index": index,
                "task": task,
                "row": row,
                **_generation_state(row),
                **_eval_state(row),
            }
            observed_attempts.append(observed)
            if round_number > max_rounds:
                continue
            path_text = str(path)
            if path_text not in used_reports:
                used_reports.append(path_text)
            attempts.append(observed)
    token_cost = _load_json(token_cost_path) if token_cost_path else {}
    patch_sha_by_task: dict[str, str] = {}
    eval_attempts_by_task: dict[str, int] = {}
    indices_by_task: dict[str, set[int]] = defaultdict(set)
    tasks_by_index: dict[int, set[str]] = defaultdict(set)
    for attempt in observed_attempts:
        task = attempt["task"]
        index = attempt.get("index")
        if isinstance(index, int):
            indices_by_task[task].add(index)
            tasks_by_index[index].add(task)
        for reason in attempt.get("identity_reasons") or []:
            _integrity.append_issue(task_issues, task, str(reason))
        patch_sha = str(attempt.get("patch_sha256") or "")
        if _is_declared_empty_patch(attempt) or _is_pending_dry_run(attempt):
            pass
        elif patch_sha:
            previous = patch_sha_by_task.get(task)
            if previous and previous != patch_sha:
                _integrity.append_issue(task_issues, task, "candidate_identity_mismatch")
            patch_sha_by_task[task] = patch_sha
        else:
            _integrity.append_issue(task_issues, task, "missing_candidate_identity")
        eval_attempts_by_task[task] = (
            eval_attempts_by_task.get(task, 0) + _eval_attempt_count(attempt["row"])
        )
    for task, indices in indices_by_task.items():
        if len(indices) > 1:
            _integrity.append_issue(task_issues, task, "task_index_mapping_conflict")
            for index in indices:
                _integrity.append_issue(issues_by_index, index, "task_index_mapping_conflict")
    for index, mapped_tasks in tasks_by_index.items():
        if len(mapped_tasks) > 1:
            _integrity.append_issue(issues_by_index, index, "index_task_mapping_conflict")
            for task in mapped_tasks:
                _integrity.append_issue(task_issues, task, "index_task_mapping_conflict")

    expected_values = expected_indices if expected_indices is not None else inferred_indices
    expected_list: list[int] = []
    for value in expected_values:
        index = _integrity.strict_index(value)
        if index is None:
            if "invalid_expected_index_census" not in global_census_issues:
                global_census_issues.append("invalid_expected_index_census")
            continue
        if index in expected_list:
            _integrity.append_issue(issues_by_index, index, "duplicate_expected_index")
            continue
        expected_list.append(index)
    expected = tuple(expected_list)
    verdicts: dict[tuple[str, str], set[bool]] = defaultdict(set)
    for attempt in observed_attempts:
        if attempt.get("eval_success") and attempt.get("patch_sha256"):
            verdicts[(attempt["task"], attempt["patch_sha256"])].add(
                bool(attempt["resolved"])
            )
    for (task, _patch_sha256), values in verdicts.items():
        if values == {False, True}:
            _integrity.append_issue(task_issues, task, "conflicting_eval_verdicts")
    exhausted = {
        task: count
        for task, count in eval_attempts_by_task.items()
        if count > max_eval_attempts
    }
    if exhausted and not allow_over_budget_evidence:
        details = ", ".join(f"{task}={count}" for task, count in sorted(exhausted.items()))
        raise ValueError(
            f"eval attempt budget exceeded (max {max_eval_attempts}): {details}"
        )
    accepted_attempts = attempts
    over_budget_evidence: dict[str, list[dict[str, Any]]] = {}
    if exhausted:
        accepted_attempts = []
        accepted_eval_attempts: dict[str, int] = {}
        exhausted_tasks = set(exhausted)
        for attempt in observed_attempts:
            task = attempt["task"]
            if task not in exhausted_tasks:
                if attempt["round"] <= max_rounds:
                    accepted_attempts.append(attempt)
                continue
            next_count = accepted_eval_attempts.get(task, 0) + _eval_attempt_count(attempt["row"])
            if next_count > max_eval_attempts:
                over_budget_evidence.setdefault(task, []).append(attempt)
                continue
            accepted_eval_attempts[task] = next_count
            accepted_attempts.append(attempt)
    cost_by_task = _assign_costs([attempt["row"] for attempt in accepted_attempts], token_cost)

    final_by_task: dict[str, dict[str, Any]] = {}
    for attempt in accepted_attempts:
        task = attempt["task"]
        if not task:
            continue
        previous = final_by_task.get(task)
        if previous is None or _row_score(
            attempt["row"], attempt["round"]
        ) >= _row_score(previous["row"], previous["round"]):
            final_by_task[task] = attempt

    tasks = []
    ordered_tasks = sorted(
        final_by_task.items(),
        key=lambda item: (
            item[1]["index"] is None,
            item[1]["index"] or 0,
            item[0],
        ),
    )
    for task, final in ordered_tasks:
        cost = cost_by_task.get(task, {})
        cost_usd = cost.get("cost_usd")
        task_record = {
            key: value
            for key, value in final.items()
            if key not in {"row"}
        }
        task_attempts = sorted(
            (attempt for attempt in accepted_attempts if attempt["task"] == task),
            key=lambda attempt: (
                str(attempt.get("source_report") or ""),
                int(attempt.get("round") or 0),
                str(attempt.get("eval_status") or ""),
                str(attempt.get("resolved")),
            ),
        )
        task_record["attempt_count"] = len(task_attempts)
        task_record["eval_attempt_count"] = sum(_eval_attempt_count(attempt["row"]) for attempt in task_attempts)
        task_record["attempts"] = [_compact_attempt(attempt) for attempt in task_attempts]
        observed_attempts_for_task = [attempt for attempt in observed_attempts if attempt["task"] == task]
        task_record["observed_record_count"] = len(observed_attempts_for_task)
        task_record["observed_eval_attempt_count"] = eval_attempts_by_task.get(task, 0)
        if task in over_budget_evidence:
            task_record["accepted_eval_status"] = task_record["eval_status"]
            task_record["accepted_resolved"] = task_record["resolved"]
            task_record["eval_status"] = "over_budget_evidence"
            task_record["eval_success"] = False
            task_record["eval_pending"] = False
            task_record["resolved"] = None
            task_record["technical_failed"] = True
            task_record["technical_reasons"] = ["eval_attempt_budget_exceeded"]
            task_record["over_budget_evidence"] = [
                _compact_attempt(attempt)
                for attempt in sorted(
                    over_budget_evidence[task],
                    key=lambda item: (
                        str(item.get("source_report") or ""),
                        int(item.get("round") or 0),
                    ),
                )
            ]
        if task_issues.get(task):
            task_record = _integrity.mark_technical(task_record, task_issues[task])
        task_record["token_cost"] = cost
        if usd_cny is not None and cost_usd is not None:
            task_record["token_cost"]["usd_cny"] = usd_cny
            task_record["token_cost"]["cost_cny"] = round(float(cost_usd) * usd_cny, 6)
        tasks.append(task_record)

    tasks = _integrity.apply_expected_census(
        tasks,
        expected,
        issues_by_index,
        global_census_issues,
    )

    counts = {
        "tasks": len(tasks),
        "attempts": len(accepted_attempts),
        "eval_attempts": sum(int(task.get("eval_attempt_count") or 0) for task in tasks),
        "observed_eval_attempts": sum(eval_attempts_by_task.values()),
        "over_budget_tasks": len(over_budget_evidence),
        "over_budget_eval_attempts": sum(
            _eval_attempt_count(attempt["row"])
            for task_attempts in over_budget_evidence.values()
            for attempt in task_attempts
        ),
        "eval_retry_tasks": sum(1 for task in tasks if int(task.get("eval_attempt_count") or 0) > 1),
        "eval_success": sum(1 for task in tasks if task["eval_success"]),
        "empty_patch": sum(
            1 for task in tasks if task.get("generation_status") == "empty_patch"
        ),
        "eval_pending": sum(1 for task in tasks if task["eval_pending"]),
        "eval_failed": sum(
            1 for task in tasks if not task["eval_success"] and not task["eval_pending"]
        ),
        "resolved": sum(1 for task in tasks if task.get("resolved") is True),
        "unresolved": sum(1 for task in tasks if task.get("resolved") is False),
        "technical_failed_final": sum(1 for task in tasks if task.get("technical_failed")),
        "rounds": max(rounds_by_task.values(), default=0),
    }
    return {
        "schema": "opencollab.swe_eval_layer_final_report.v1",
        "generated_at": _now(),
        "max_rounds": max_rounds,
        "max_eval_attempts": max_eval_attempts,
        "allow_over_budget_evidence": allow_over_budget_evidence,
        "source_reports": [str(path) for path in ordered_report_paths],
        "used_source_reports": sorted(used_reports),
        "expected_indices": list(expected),
        "census_errors": global_census_issues,
        "token_cost_summary": str(token_cost_path) if token_cost_path else None,
        "counts": counts,
        "tasks": tasks,
    }


def to_markdown(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    lines = [
        "# SWE Eval Layer Final Report",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- rounds: `{counts.get('rounds')}`",
        f"- tasks: `{counts.get('tasks')}`",
        f"- attempts: `{counts.get('attempts')}`",
        f"- eval_attempts: `{counts.get('eval_attempts')}`",
        f"- observed_eval_attempts: `{counts.get('observed_eval_attempts')}`",
        f"- over_budget_tasks: `{counts.get('over_budget_tasks')}`",
        f"- over_budget_eval_attempts: `{counts.get('over_budget_eval_attempts')}`",
        f"- eval_retry_tasks: `{counts.get('eval_retry_tasks')}`",
        f"- eval_success: `{counts.get('eval_success')}`",
        f"- empty_patch: `{counts.get('empty_patch')}`",
        f"- eval_pending: `{counts.get('eval_pending')}`",
        f"- eval_failed: `{counts.get('eval_failed')}`",
        f"- resolved: `{counts.get('resolved')}`",
        f"- unresolved: `{counts.get('unresolved')}`",
        f"- technical_failed_final: `{counts.get('technical_failed_final')}`",
        "",
        "| idx | task | rounds | eval_attempts | eval | resolved | tokens | cost_usd | cost_cny | report |",
        "| --- | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for task in report.get("tasks") or []:
        token_cost = task.get("token_cost") if isinstance(task.get("token_cost"), dict) else {}
        lines.append(
            (
                "| {idx} | `{task}` | {attempts} | {eval_attempts} | "
                "`{eval_status}` | `{resolved}` | {tokens} | {cost_usd} | "
                "{cost_cny} | `{report}` |"
            ).format(
                idx=task.get("index"),
                task=task.get("task"),
                attempts=task.get("attempt_count"),
                eval_attempts=task.get("eval_attempt_count"),
                eval_status=task.get("eval_status"),
                resolved=task.get("resolved"),
                tokens=token_cost.get("workflow_tokens") or 0,
                cost_usd="" if token_cost.get("cost_usd") is None else token_cost.get("cost_usd"),
                cost_cny="" if token_cost.get("cost_cny") is None else token_cost.get("cost_cny"),
                report=task.get("report_path") or "",
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge up to two SWE eval rounds into a final fact report.")
    parser.add_argument("--report-json", action="append", required=True)
    parser.add_argument("--expected-index", action="append", type=int, default=[])
    parser.add_argument("--token-cost-json", type=Path)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--max-eval-attempts", type=int, default=2)
    parser.add_argument("--allow-over-budget-evidence", action="store_true")
    parser.add_argument("--usd-cny", type=float)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    report = build_report(
        [Path(path) for path in args.report_json],
        token_cost_path=args.token_cost_json,
        expected_indices=args.expected_index or None,
        max_rounds=args.max_rounds,
        max_eval_attempts=args.max_eval_attempts,
        allow_over_budget_evidence=args.allow_over_budget_evidence,
        usd_cny=args.usd_cny,
    )
    _report_io.write_json(args.json_output, report)
    _report_io.write_text(args.markdown_output, to_markdown(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
