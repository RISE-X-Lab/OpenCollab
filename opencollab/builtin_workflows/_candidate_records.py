"""Exact public test comparison and candidate adoption for Duo."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import CandidateRun

MAX_PUBLIC_COMMAND_BYTES = 4_000


def _clip_text(value: Any, limit: int) -> str:
    """Preserve complete model-visible evidence, including identity-bearing text."""
    del limit
    text = "" if value is None else str(value)
    return text


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": _clip_text(record.get("target"), 2_000),
        "runner": _clip_text(record.get("runner"), 200),
        "command": _clip_text(record.get("command"), MAX_PUBLIC_COMMAND_BYTES),
        "exit_code": record.get("exit_code"),
        "verified": record.get("verified") is True,
    }


def _candidate_output(candidate: CandidateRun) -> dict[str, Any]:
    return candidate.output if isinstance(candidate.output, dict) else {}


def _candidate_command(candidate: CandidateRun) -> str:
    return str(_candidate_output(candidate).get("public_command") or "").strip()


def _candidate_records(candidate: CandidateRun) -> list[dict[str, Any]]:
    records = _candidate_output(candidate).get("public_test_records")
    if not isinstance(records, list):
        return []
    return [_public_record(record) for record in records if isinstance(record, dict)]


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("target") or "").strip(),
        str(record.get("runner") or "").strip(),
        str(record.get("command") or "").strip(),
    )


def _record_state(record: dict[str, Any]) -> str | None:
    exit_code = record.get("exit_code")
    if not all(_record_key(record)) or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    if exit_code == 0 and record.get("verified") is True:
        return "green"
    if exit_code != 0 and record.get("verified") is False:
        return "red"
    return None


def _public_red_winner(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> str | None:
    states_a = {
        _record_key(record): state
        for record in _candidate_records(candidate_a)
        if (state := _record_state(record)) is not None
    }
    states_b = {
        _record_key(record): state
        for record in _candidate_records(candidate_b)
        if (state := _record_state(record)) is not None
    }
    decisions = {
        "A" if states_a[key] == "green" else "B"
        for key in states_a.keys() & states_b.keys()
        if {states_a[key], states_b[key]} == {"green", "red"}
    }
    return next(iter(decisions)) if len(decisions) == 1 else None


async def _adopt(
    ctx: Any,
    *,
    winner: str | None,
    candidates: dict[str, CandidateRun],
    preserve_paths: list[str],
) -> tuple[str | None, list[str]]:
    if winner is None:
        return None, []
    order = [winner, *(label for label in ("A", "B") if label != winner)]
    attempts: list[str] = []
    for label in order:
        candidate = candidates[label]
        if not candidate.diff.strip():
            continue
        attempts.append(label)
        try:
            await ctx.adopt_candidate(candidate, preserve_paths=preserve_paths)
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"Duo adoption failed ({label}) after {type(exc).__name__}")
            continue
        return label, attempts
    return None, attempts
