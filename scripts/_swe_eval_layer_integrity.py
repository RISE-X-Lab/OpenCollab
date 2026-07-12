"""Identity and execution-proof checks for SWE evaluation fact reports."""

from __future__ import annotations

import hashlib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "opencollab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "opencollab"))

from opencollab.harness.swe_eval_discovery import (  # noqa: E402
    _direct_eval_done_has_execution_proof,
)
from opencollab.harness.swe_generation_proof import (  # noqa: E402
    current_generation_summary_proof_valid,
)

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
EMPTY_PATCH_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class AttemptIntegrity:
    """Normalized identity and direct-execution evidence for one task row."""

    record_id: str
    patch_sha256: str
    direct_execution_proven: bool
    reasons: tuple[str, ...]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _full_sha(value: Any) -> str:
    text = str(value or "")
    return text.lower() if _SHA256_RE.fullmatch(text) else ""


def _append_once(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _empty_patch_integrity(row: dict[str, Any], task: str) -> AttemptIntegrity:
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    reasons: list[str] = []
    if generation.get("status") != "empty_patch":
        reasons.append("empty_patch_generation_status_invalid")
    patch_len = generation.get("patch_len")
    if isinstance(patch_len, bool) or patch_len != 0:
        reasons.append("empty_patch_length_invalid")
    if str(generation.get("task") or "") != task:
        reasons.append("generation_task_mismatch")
    record_id = str(generation.get("record_id") or "")
    if not record_id:
        reasons.append("missing_generation_record_id")
    patch_sha256 = _full_sha(generation.get("patch_sha256"))
    if patch_sha256 != EMPTY_PATCH_SHA256:
        reasons.append("empty_patch_sha256_invalid")
    if generation.get("workflow_status") != "empty_patch_after_done":
        reasons.append("empty_patch_workflow_status_invalid")
    if generation.get("submission_integrity") != "empty_patch_proven":
        reasons.append("empty_patch_integrity_unproven")
    expected_integrity_fields = {
        "submission_eligible": False,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": False,
    }
    for field, expected in expected_integrity_fields.items():
        if generation.get(field) is not expected:
            reasons.append(f"empty_patch_integrity_field_invalid:{field}")
    extraction = _mapping(generation.get("trusted_patch_extraction"))
    if (
        not current_generation_summary_proof_valid(generation)
        or extraction.get("patch_bytes") != 0
    ):
        reasons.append("missing_trusted_empty_patch_proof")
    if str(evaluation.get("task") or "") != task:
        reasons.append("evaluation_task_mismatch")
    if evaluation.get("status") != "skipped_empty_patch":
        reasons.append("empty_patch_eval_status_invalid")
    if evaluation.get("summary") not in (None, {}):
        reasons.append("empty_patch_eval_summary_not_empty")
    if evaluation.get("executed") is True:
        reasons.append("empty_patch_eval_execution_conflict")
    if "attempt_count" in evaluation:
        attempt_count = evaluation.get("attempt_count")
        if isinstance(attempt_count, bool) or attempt_count != 0:
            reasons.append("empty_patch_eval_attempt_count_invalid")
    return AttemptIntegrity(
        record_id=record_id,
        patch_sha256=patch_sha256,
        direct_execution_proven=False,
        reasons=tuple(reasons),
    )


def declared_empty_patch(row: dict[str, Any]) -> bool:
    task = str(row.get("task") or row.get("instance_id") or "")
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    claims_empty = bool(
        generation.get("status") == "empty_patch"
        or evaluation.get("status") == "skipped_empty_patch"
    )
    return bool(claims_empty and not _empty_patch_integrity(row, task).reasons)


def attempt_integrity(row: dict[str, Any], task: str) -> AttemptIntegrity:
    """Require a full generation/evaluation identity and executable terminal proof."""
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    generation_status = str(generation.get("status") or "")
    eval_status = str(evaluation.get("status") or "")
    if generation_status == "would_generate" and eval_status == "would_eval":
        return AttemptIntegrity("", "", False, ())
    if (
        generation_status == "empty_patch"
        or eval_status == "skipped_empty_patch"
    ):
        return _empty_patch_integrity(row, task)
    summary = _mapping(evaluation.get("summary"))
    reasons: list[str] = []

    if generation_status != "generation_done":
        _append_once(
            reasons,
            "missing_generation_status"
            if not generation_status
            else f"unexpected_generation_status:{generation_status}",
        )
    if str(generation.get("task") or "") != task:
        _append_once(reasons, "generation_task_mismatch")
    record_id = str(generation.get("record_id") or "")
    if not record_id:
        _append_once(reasons, "missing_generation_record_id")
    generation_sha = _full_sha(generation.get("patch_sha256"))
    if not generation_sha:
        _append_once(reasons, "invalid_generation_patch_sha256")
    if not current_generation_summary_proof_valid(generation):
        _append_once(reasons, "missing_trusted_generation_proof")

    if evaluation and str(evaluation.get("task") or "") != task:
        _append_once(reasons, "evaluation_task_mismatch")

    terminal_or_recorded = bool(summary) or eval_status in {
        "eval_done",
        "technical_eval_failed",
        "empty_eval_patch_invalid",
        "blocked_missing_eval_spec",
    }
    summary_record_id = str(summary.get("record_id") or "")
    summary_sha = _full_sha(summary.get("patch_sha256"))
    if terminal_or_recorded:
        if str(summary.get("task") or "") != task:
            _append_once(reasons, "eval_summary_task_mismatch")
        if not summary_record_id:
            _append_once(reasons, "missing_eval_record_id")
        elif record_id and summary_record_id != record_id:
            _append_once(reasons, "eval_record_id_mismatch")
        if not summary_sha:
            _append_once(reasons, "invalid_eval_patch_sha256")
        elif generation_sha and summary_sha != generation_sha:
            _append_once(reasons, "eval_patch_sha256_mismatch")

    direct_execution_proven = bool(
        eval_status == "eval_done"
        and _direct_eval_done_has_execution_proof(summary)
    )
    if eval_status == "eval_done" and not direct_execution_proven:
        _append_once(reasons, "missing_direct_execution_proof")
    if eval_status == "technical_eval_failed" and summary.get("resolved") is True:
        _append_once(reasons, "technical_resolved_conflict")

    return AttemptIntegrity(
        record_id=record_id,
        patch_sha256=generation_sha or summary_sha,
        direct_execution_proven=direct_execution_proven,
        reasons=tuple(reasons),
    )


def strict_index(value: Any) -> int | None:
    """Return an integer task index while rejecting booleans and lossy strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value)
    return None


def append_issue(container: dict[Any, list[str]], key: Any, reason: str) -> None:
    """Add one deterministic technical reason without duplicates."""
    reasons = container.setdefault(key, [])
    if reason not in reasons:
        reasons.append(reason)


def report_census(
    report: dict[str, Any],
) -> tuple[list[int], dict[int, list[str]], list[str]]:
    """Extract the declared task census and orchestrator-level failures."""
    expected: list[int] = []
    issues: dict[int, list[str]] = {}
    global_issues: list[str] = []
    raw_indices = report.get("indices")
    if raw_indices is not None:
        if not isinstance(raw_indices, list):
            global_issues.append("invalid_expected_index_census")
        else:
            for value in raw_indices:
                index = strict_index(value)
                if index is None:
                    if "invalid_expected_index_census" not in global_issues:
                        global_issues.append("invalid_expected_index_census")
                    continue
                if index in expected:
                    append_issue(issues, index, "duplicate_expected_index")
                else:
                    expected.append(index)

    results = report.get("results")
    if not isinstance(results, list):
        return expected, issues, global_issues
    seen_results: set[int] = set()
    for result in results:
        if not isinstance(result, dict):
            if "invalid_orchestrator_result" not in global_issues:
                global_issues.append("invalid_orchestrator_result")
            continue
        index = strict_index(result.get("index"))
        if index is None:
            if "invalid_orchestrator_result_index" not in global_issues:
                global_issues.append("invalid_orchestrator_result_index")
            continue
        if index in seen_results:
            append_issue(issues, index, "duplicate_orchestrator_result")
        seen_results.add(index)
        if str(result.get("runner_status") or "") == "orchestrator_exception":
            append_issue(issues, index, "orchestrator_exception")
        if result.get("completed") is False:
            append_issue(issues, index, "incomplete_orchestrator_result")
        rows = result.get("rows")
        if not isinstance(rows, list) or not rows:
            append_issue(issues, index, "missing_task_row")
            continue
        for row in rows:
            if not isinstance(row, dict):
                append_issue(issues, index, "invalid_task_row")
                continue
            row_index = strict_index(row.get("index"))
            if row_index != index:
                append_issue(issues, index, "orchestrator_row_index_mismatch")
                if row_index is not None:
                    append_issue(issues, row_index, "orchestrator_row_index_mismatch")
    return expected, issues, global_issues


def mark_technical(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    """Convert one selected fact row into a fail-closed technical result."""
    value = dict(record)
    previous_reasons = [str(reason) for reason in value.get("technical_reasons") or []]
    for reason in reasons:
        if reason not in previous_reasons:
            previous_reasons.append(reason)
    if value.get("eval_status") != "technical_report_inconsistency":
        value["accepted_eval_status"] = value.get("eval_status")
        value["accepted_resolved"] = value.get("resolved")
    value.update(
        {
            "eval_status": "technical_report_inconsistency",
            "eval_success": False,
            "eval_pending": False,
            "resolved": None,
            "technical_failed": True,
            "technical_reasons": previous_reasons,
        }
    )
    return value


def _missing_task_record(index: int, reasons: list[str]) -> dict[str, Any]:
    return mark_technical(
        {
            "round": 0,
            "source_order": 0,
            "source_report": None,
            "index": index,
            "task": f"expected-index-{index}",
            "generation_status": "missing_generation",
            "patch_len": None,
            "patch_sha256": "",
            "record_id": "",
            "direct_execution_proven": False,
            "report_path": None,
            "attempt_count": 0,
            "eval_attempt_count": 0,
            "attempts": [],
            "observed_record_count": 0,
            "observed_eval_attempt_count": 0,
            "token_cost": {},
            "technical_reasons": [],
        },
        ["missing_expected_task", *reasons],
    )


def apply_expected_census(
    tasks: list[dict[str, Any]],
    expected_indices: tuple[int, ...],
    issues_by_index: dict[int, list[str]],
    global_issues: list[str],
) -> list[dict[str, Any]]:
    """Return exactly one fail-closed task row for every expected index."""
    if not expected_indices:
        return tasks
    by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        index = strict_index(task.get("index"))
        if index is not None:
            by_index[index].append(task)

    expected_set = set(expected_indices)
    if any(index not in expected_set for index in by_index):
        if "unexpected_task_index" not in global_issues:
            global_issues.append("unexpected_task_index")

    result: list[dict[str, Any]] = []
    for index in expected_indices:
        candidates = sorted(by_index.get(index, []), key=lambda item: str(item.get("task") or ""))
        reasons = [*global_issues, *issues_by_index.get(index, [])]
        if not candidates:
            result.append(_missing_task_record(index, reasons))
            continue
        selected = candidates[0]
        if len(candidates) > 1:
            reasons.append("index_task_mapping_conflict")
            selected = dict(selected)
            selected["mapping_conflict_tasks"] = [item.get("task") for item in candidates]
        result.append(mark_technical(selected, reasons) if reasons else selected)
    return result


__all__ = [
    "AttemptIntegrity",
    "EMPTY_PATCH_SHA256",
    "append_issue",
    "apply_expected_census",
    "attempt_integrity",
    "declared_empty_patch",
    "mark_technical",
    "report_census",
    "strict_index",
]
