"""Machine summary builder for SWE evaluation records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from opencollab.harness.eval_adapter.models import RunRecord


def build_eval_summary(
    records: list[RunRecord],
    *,
    run_id: str,
    solver: str,
    usd_cny: float | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        grouped[record.task_id].append(record)

    rows: list[dict[str, Any]] = []
    totals = {
        "tasks": len(grouped),
        "generation_done": 0,
        "empty_patch": 0,
        "official_eval_done": 0,
        "resolved": 0,
        "unresolved": 0,
        "technical_failed": 0,
        "missing_eval": 0,
        "retry_tasks": 0,
    }
    total_tokens = 0
    total_cost_usd = 0.0

    for task_id in sorted(grouped):
        attempts = sorted(grouped[task_id], key=lambda item: item.attempt)
        final = attempts[-1]
        candidate = final.candidate
        eval_result = final.eval_result
        classification = _classification(final)
        token_count = sum(int(item.candidate.token_count) for item in attempts if item.candidate)
        cost_usd = sum(float(item.candidate.cost_usd) for item in attempts if item.candidate)
        total_tokens += token_count
        total_cost_usd += cost_usd

        if candidate is not None:
            totals["generation_done"] += 1
            if candidate.is_empty:
                totals["empty_patch"] += 1
        if eval_result is not None and eval_result.eval_done:
            totals["official_eval_done"] += 1
        if classification == "resolved":
            totals["resolved"] += 1
        elif classification == "unresolved":
            totals["unresolved"] += 1
        elif classification == "technical_failed":
            totals["technical_failed"] += 1
        elif classification == "missing_eval":
            totals["missing_eval"] += 1
        if len(attempts) > 1:
            totals["retry_tasks"] += 1

        rows.append(
            {
                "task_id": task_id,
                "solver": final.solver_name,
                "attempts": len(attempts),
                "final_attempt": final.attempt,
                "final_classification": classification,
                "patch_sha256": candidate.patch_sha256 if candidate else "",
                "empty_patch": bool(candidate.is_empty) if candidate else False,
                "eval_done": bool(eval_result.eval_done) if eval_result else False,
                "resolved": eval_result.resolved if eval_result else None,
                "technical_failed": bool(eval_result.technical_failed) if eval_result else False,
                "technical_reasons": list(eval_result.technical_reasons) if eval_result else [],
                "tokens": token_count,
                "cost_usd": round(cost_usd, 8),
                "generation_log": candidate.log_path if candidate else "",
                "eval_report": eval_result.report_path if eval_result else "",
                "eval_log": eval_result.log_path if eval_result else "",
            }
        )

    token_cost: dict[str, Any] = {
        "total_tokens": total_tokens,
        "cost_usd": round(total_cost_usd, 8),
    }
    if usd_cny is not None:
        token_cost["cost_cny"] = round(total_cost_usd * usd_cny, 4)
        token_cost["usd_cny"] = usd_cny

    return {
        "run_id": run_id,
        "solver": solver,
        "counts": totals,
        "token_cost": token_cost,
        "rows": rows,
    }


def _classification(record: RunRecord) -> str:
    candidate = record.candidate
    eval_result = record.eval_result
    if candidate is None:
        return "missing_generation"
    if candidate.is_empty:
        return "empty_patch"
    if eval_result is None:
        return "missing_eval"
    if eval_result.technical_failed:
        return "technical_failed"
    if eval_result.eval_done and eval_result.resolved:
        return "resolved"
    if eval_result.eval_done:
        return "unresolved"
    return "missing_eval"


__all__ = ["build_eval_summary"]
