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
        "requirements_complete": {"type": "boolean"},
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
    if candidate_a.diff == candidate_b.diff:
        return "A", "identical-diff", False
    public_winner = dual._public_red_winner(candidate_a, candidate_b)
    if public_winner is not None:
        return public_winner, "same-command-public-red", False
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


def _validated_judge_winner(
    result: Any,
    paths: dict[str, list[str]],
) -> str | None:
    if not isinstance(result, dict) or result.get("requirements_complete") is not True:
        return None
    winner = result.get("winner")
    requirements = result.get("requirements")
    if winner not in {"A", "B"} or not isinstance(requirements, list):
        return None
    if not 1 <= len(requirements) <= MAX_REQUIREMENTS:
        return None
    loser = "B" if winner == "A" else "A"
    advantage = False
    for item in requirements:
        if not isinstance(item, dict) or not str(item.get("requirement") or "").strip():
            return None
        coverage_a = item.get("a_coverage")
        coverage_b = item.get("b_coverage")
        evidence_a = item.get("a_evidence")
        evidence_b = item.get("b_evidence")
        if (
            coverage_a not in COVERAGE_VALUES
            or coverage_b not in COVERAGE_VALUES
            or not isinstance(evidence_a, list)
            or not isinstance(evidence_b, list)
        ):
            return None
        coverage = {"A": coverage_a, "B": coverage_b}
        evidence = {"A": evidence_a, "B": evidence_b}
        if coverage[loser] == "covered" and coverage[winner] == "not_covered":
            return None
        if coverage[winner] == "covered" and coverage[loser] == "not_covered":
            if not _evidence_mentions_changed_path(evidence[winner], paths[winner]):
                return None
            advantage = True
    return winner if advantage else None
