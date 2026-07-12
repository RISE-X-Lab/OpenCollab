"""Machine summary builder for SWE evaluation records."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from opencollab.harness.eval_adapter.models import RunRecord

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


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
        evidence = _record_evidence(final)
        if _has_conflicting_verdicts(attempts):
            evidence["classification"] = "technical_failed"
            evidence["technical_failed"] = True
            evidence["resolved"] = None
            evidence["technical_reasons"] = [
                *evidence["technical_reasons"],
                "conflicting_eval_verdicts",
            ]
        classification = str(evidence["classification"])
        accepted_eval_done = classification in {"resolved", "unresolved"}
        token_count = sum(int(item.candidate.token_count) for item in attempts if item.candidate)
        cost_usd = sum(float(item.candidate.cost_usd) for item in attempts if item.candidate)
        total_tokens += token_count
        total_cost_usd += cost_usd

        if candidate is not None:
            totals["generation_done"] += 1
            if candidate.is_empty:
                totals["empty_patch"] += 1
        if accepted_eval_done:
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
                "eval_done": accepted_eval_done,
                "resolved": evidence["resolved"],
                "technical_failed": evidence["technical_failed"],
                "technical_reasons": evidence["technical_reasons"],
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


def _identity_reasons(record: RunRecord) -> list[str]:
    candidate = record.candidate
    eval_result = record.eval_result
    reasons: list[str] = []
    if candidate is not None and candidate.task_id != record.task_id:
        reasons.append("candidate_task_mismatch")
    if candidate is None or eval_result is None:
        return reasons
    if candidate.is_empty and (eval_result.eval_done or eval_result.resolved):
        reasons.append("empty_candidate_eval_conflict")
    if eval_result.task_id != record.task_id:
        reasons.append("eval_task_mismatch")
    if _SHA256_RE.fullmatch(str(eval_result.patch_sha256 or "")) is None:
        reasons.append("eval_patch_sha_invalid")
    elif eval_result.patch_sha256.lower() != candidate.patch_sha256.lower():
        reasons.append("eval_patch_sha_mismatch")
    if eval_result.technical_failed and (eval_result.eval_done or eval_result.resolved):
        reasons.append("technical_resolved_conflict")
    if eval_result.resolved and not eval_result.eval_done:
        reasons.append("resolved_without_completed_eval")
    return reasons


def _record_evidence(record: RunRecord) -> dict[str, Any]:
    candidate = record.candidate
    eval_result = record.eval_result
    reasons = _identity_reasons(record)
    if candidate is None:
        return _evidence("missing_generation", reasons=reasons)
    if eval_result is None:
        if reasons:
            return _evidence("technical_failed", reasons=reasons, technical_failed=True)
        if candidate.is_empty:
            return _evidence("empty_patch")
        return _evidence("missing_eval", reasons=reasons)
    reasons.extend(reason for reason in eval_result.technical_reasons if reason not in reasons)
    if reasons or eval_result.technical_failed:
        return _evidence("technical_failed", reasons=reasons, technical_failed=True)
    if candidate.is_empty:
        return _evidence("empty_patch")
    if eval_result.eval_done and eval_result.resolved:
        return _evidence("resolved", resolved=True)
    if eval_result.eval_done:
        return _evidence("unresolved", resolved=False)
    return _evidence("missing_eval")


def _evidence(
    classification: str,
    *,
    reasons: list[str] | None = None,
    resolved: bool | None = None,
    technical_failed: bool = False,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "resolved": resolved,
        "technical_failed": technical_failed,
        "technical_reasons": list(reasons or ()),
    }


def _has_conflicting_verdicts(attempts: list[RunRecord]) -> bool:
    verdicts_by_patch: dict[str, set[bool]] = defaultdict(set)
    for record in attempts:
        candidate = record.candidate
        eval_result = record.eval_result
        if (
            candidate is None
            or candidate.is_empty
            or eval_result is None
            or _identity_reasons(record)
            or eval_result.technical_failed
            or not eval_result.eval_done
        ):
            continue
        verdicts_by_patch[candidate.patch_sha256].add(eval_result.resolved)
    return any(verdicts == {False, True} for verdicts in verdicts_by_patch.values())


__all__ = ["build_eval_summary"]
