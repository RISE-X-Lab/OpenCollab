"""Mechanical candidate comparison and structured public-evidence decisions."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from opencollab.patches import patch_paths
from opencollab.workflows import CandidateRun

from . import _candidate_records as dual

MAX_DIFF_BYTES = 240_000


MAX_JUDGE_INPUT_BYTES = 600_000


MAX_REQUIREMENTS = 64


COVERAGE_VALUES = frozenset({"covered", "not_covered", "unclear"})


CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": ["A", "B"]},
        "requirements_complete": {
            "type": "boolean",
            "description": (
                "True when the requirements array accounts for every explicit task requirement. "
                "This measures inventory completeness, not candidate correctness or test success. "
                "Keep true even when either or both candidates have not_covered or unclear entries; "
                "use false only for an incomplete requirement inventory."
            ),
        },
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_REQUIREMENTS,
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "a_coverage": {
                        "type": "string",
                        "enum": sorted(COVERAGE_VALUES),
                    },
                    "b_coverage": {
                        "type": "string",
                        "enum": sorted(COVERAGE_VALUES),
                    },
                    "a_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "b_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "requirement",
                    "a_coverage",
                    "b_coverage",
                    "a_evidence",
                    "b_evidence",
                ],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["winner", "requirements_complete", "requirements", "rationale"],
    "additionalProperties": False,
}


def _clip_text(value: Any, limit: int) -> tuple[str, bool]:
    """Preserve complete model-visible evidence, including identity-bearing text."""
    del limit
    text = "" if value is None else str(value)
    return text, False


def _mechanical_choice(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str | None, str, bool]:
    nonempty_a = bool(candidate_a.diff.strip())
    nonempty_b = bool(candidate_b.diff.strip())
    if nonempty_a and not nonempty_b:
        return "A", "only-a-nonempty", False
    if nonempty_b and not nonempty_a:
        return "B", "only-b-nonempty", False
    if not nonempty_a and not nonempty_b:
        return None, "both-empty", False
    public_winner = dual._public_red_winner(candidate_a, candidate_b)
    if public_winner is not None:
        return public_winner, "same-command-public-red", False
    if candidate_a.diff == candidate_b.diff:
        return "B", "identical-diff", False
    return None, "contract-adjudication", True


def _shared_public_records(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> list[dict[str, Any]]:
    records_a = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_a)
        if all(dual._record_key(record))
    }
    records_b = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_b)
        if all(dual._record_key(record))
    }
    return [
        {
            "target": key[0],
            "runner": key[1],
            "command": key[2],
            "A": {
                "exit_code": records_a[key].get("exit_code"),
                "verified": records_a[key].get("verified") is True,
            },
            "B": {
                "exit_code": records_b[key].get("exit_code"),
                "verified": records_b[key].get("verified") is True,
            },
        }
        for key in sorted(records_a.keys() & records_b.keys())
    ]


def _candidate_view(candidate: CandidateRun) -> tuple[dict[str, Any], bool]:
    diff, truncated = _clip_text(candidate.diff, MAX_DIFF_BYTES)
    return {
        "diff": diff,
        "diff_truncated": truncated,
        "changed_paths": patch_paths(candidate.diff),
        "public_command": dual._candidate_command(candidate),
    }, truncated


def _judge_input(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str, dict[str, list[str]], bool]:
    view_a, truncated_a = _candidate_view(candidate_a)
    view_b, truncated_b = _candidate_view(candidate_b)
    payload = {
        "A": view_a,
        "B": view_b,
        "shared_public_test_records": _shared_public_records(
            candidate_a,
            candidate_b,
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    text, payload_truncated = _clip_text(encoded, MAX_JUDGE_INPUT_BYTES)
    paths = {
        "A": list(view_a["changed_paths"]),
        "B": list(view_b["changed_paths"]),
    }
    return text, paths, truncated_a or truncated_b or payload_truncated


def _evidence_mentions_changed_path(
    evidence: list[Any],
    paths: list[str],
) -> bool:
    anchors = [str(item).replace("\\", "/") for item in evidence if str(item)]
    for path in paths:
        normalized = str(path).replace("\\", "/")
        name = PurePosixPath(normalized).name
        if any(normalized in anchor or name and name in anchor for anchor in anchors):
            return True
    return False


def _judge_issue(
    result: Any,
    paths: dict[str, list[str]],
) -> str | None:
    """Explain an unusable recommendation so a bounded review can reconcile it."""
    if not isinstance(result, dict):
        return "invalid_decision"
    if result.get("requirements_complete") is not True:
        return "incomplete_requirement_inventory"
    winner = result.get("winner")
    requirements = result.get("requirements")
    if not isinstance(winner, str) or winner not in {"A", "B"} or not isinstance(requirements, list):
        return "invalid_decision"
    if not 1 <= len(requirements) <= MAX_REQUIREMENTS:
        return "invalid_requirement_count"
    loser = "B" if winner == "A" else "A"
    advantage = False
    tied = True
    for index, item in enumerate(requirements, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("requirement"), str)
                or not item["requirement"].strip()):
            return f"invalid_requirement_{index}"
        coverage_a = item.get("a_coverage")
        coverage_b = item.get("b_coverage")
        evidence_a = item.get("a_evidence")
        evidence_b = item.get("b_evidence")
        if (
            not isinstance(coverage_a, str) or coverage_a not in COVERAGE_VALUES
            or not isinstance(coverage_b, str) or coverage_b not in COVERAGE_VALUES
            or not isinstance(evidence_a, list)
            or not isinstance(evidence_b, list)
            or any(not isinstance(value, str) for value in evidence_a + evidence_b)
        ):
            return f"invalid_requirement_evidence_{index}"
        coverage = {"A": coverage_a, "B": coverage_b}
        evidence = {"A": evidence_a, "B": evidence_b}
        tied = tied and coverage_a == coverage_b
        if coverage[loser] == "covered" and coverage[winner] == "not_covered":
            return f"recommended_{winner}_misses_requirement_{index}"
        if coverage[winner] == "covered" and coverage[loser] in {"not_covered", "unclear"}:
            supported = _evidence_mentions_changed_path(evidence[winner], paths[winner])
            if not supported and coverage[loser] == "not_covered":
                return f"missing_changed_path_for_requirement_{index}"
            advantage = advantage or supported
    if advantage:
        return None
    return "tie" if tied else "recommendation_has_no_supported_advantage"


def _validated_judge_winner(result: Any, paths: dict[str, list[str]]) -> str | None:
    return result["winner"] if _judge_issue(result, paths) is None else None


def _fallback_winner(*results: Any) -> str:
    """Prefer B without overriding an already reported explicit B regression."""
    for result in results:
        requirements = result.get("requirements") if isinstance(result, dict) else None
        if not isinstance(requirements, list):
            continue
        for item in requirements:
            if (isinstance(item, dict) and item.get("a_coverage") == "covered"
                    and item.get("b_coverage") == "not_covered"):
                return "A"
    return "B"
