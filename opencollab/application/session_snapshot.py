"""Validation helpers for restoring and serializing session snapshots."""

from __future__ import annotations

import math
from typing import Any

from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase

# Legacy phase strings from snapshots written before the Lane S1 terminal
# collapse: the four graceful terminals folded into STOPPED and the pure-
# transitional SCHEDULED was removed. Migrating them here keeps a pre-collapse
# snapshot's disposition instead of silently degrading it to IDLE via the
# unknown-value fallback.
_LEGACY_TERMINAL_PHASES = frozenset(
    {"cancelled", "budget_exceeded", "step_limit_exceeded", "context_overflow"}
)


def _restore_phase(raw: object) -> SessionPhase:
    value = str(raw)
    if value in _LEGACY_TERMINAL_PHASES:
        return SessionPhase.STOPPED
    if value == "scheduled":  # pre-collapse enqueue phase, now folded into IDLE
        return SessionPhase.IDLE
    try:
        return SessionPhase(value)
    except ValueError:
        return SessionPhase.IDLE


def _restore_queued_external_user_turn(
    value: object, messages: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Validate a queued external turn against the restored transcript."""
    if not isinstance(value, dict) or value.get("status") != "queued":
        return None
    content = value.get("content")
    turn_id = value.get("turn_id")
    index = value.get("message_index")
    if (
        not isinstance(content, str)
        or not isinstance(turn_id, str)
        or not turn_id
        or isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(messages)
    ):
        return None
    message = messages[index]
    if message.get("role") != "user" or message.get("content") != content:
        return None
    return {
        "turn_id": turn_id,
        "status": "queued",
        "content": content,
        "message_index": index,
    }


def _restore_active_turn_start(
    value: object, messages: list[dict[str, Any]]
) -> int | None:
    """Return a valid suspended-turn answer boundary, else leave it unknown."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= len(messages) else None


def _snapshot_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _snapshot_nonnegative_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _snapshot_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _serialize_pending_row(row: PendingRow) -> dict[str, object]:
    return {
        "tool_call_id": row.tool_call_id,
        "kind": row.kind.value,
        "order": row.order,
        "ref": row.ref,
        "status": row.status.value,
        "result": row.result,
        "error": row.error,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _restore_pending_row(value: object) -> PendingRow | None:
    if not isinstance(value, dict):
        return None
    try:
        tool_call_id = str(value["tool_call_id"])
        kind = RowKind(str(value["kind"]))
        status = RowStatus(str(value.get("status", RowStatus.PENDING.value)))
        order = int(value["order"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    result = value.get("result")
    error = value.get("error")
    finished_at = value.get("finished_at")
    if status is RowStatus.PENDING:
        status = RowStatus.FAILED
        error = "deferred child interrupted by session restore"
        result = error
        finished_at = None
    return PendingRow(
        tool_call_id=tool_call_id,
        kind=kind,
        order=order,
        ref=value.get("ref"),
        status=status,
        result=str(result) if result is not None else None,
        error=str(error) if error is not None else None,
        started_at=value.get("started_at"),
        finished_at=finished_at,
    )
