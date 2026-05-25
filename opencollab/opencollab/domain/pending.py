"""Pure domain for a session's pending-event table — no I/O, no asyncio.

When a run loop emits a batch of tool calls, every call gets a row here keyed by
its ``tool_call_id``. Immediate (synchronous) tools fill their row at once;
deferrable tools (a spawned child agent, a future external job) leave a PENDING
row that the scheduler fills when the result arrives. The session resumes from
``AWAITING_EVENTS`` only once every row is filled, then drains
``ordered_results`` back into the message history as one contiguous tool-result
block — preserving the LLM protocol rule that every ``tool_call_id`` is answered
before the next model call.

Rows are frozen so the table is trivially serializable for a future durable
snapshot/restore; ``fill`` replaces a row rather than mutating it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class RowKind(Enum):
    IMMEDIATE = "immediate"
    CHILD_AGENT = "child_agent"
    EXTERNAL = "external"


class RowStatus(Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class PendingRowError(Exception):
    """Raised on an unknown ``tool_call_id`` or a double-fill — surfaced loudly
    so a misrouted completion can never silently hang a suspended session.
    """


@dataclass(frozen=True)
class PendingRow:
    tool_call_id: str
    kind: RowKind
    order: int
    ref: int | str | None = None
    status: RowStatus = RowStatus.PENDING
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class PendingEventTable:
    """Mutable container of frozen :class:`PendingRow`, keyed by tool_call_id."""

    def __init__(self) -> None:
        self.rows: dict[str, PendingRow] = {}

    def add(self, row: PendingRow) -> None:
        if row.tool_call_id in self.rows:
            raise PendingRowError(f"duplicate pending row: {row.tool_call_id}")
        self.rows[row.tool_call_id] = row

    def fill(
        self,
        tool_call_id: str,
        *,
        result: str,
        status: RowStatus = RowStatus.DONE,
        finished_at: float | None = None,
        error: str | None = None,
    ) -> None:
        row = self.rows.get(tool_call_id)
        if row is None:
            raise PendingRowError(f"no pending row for tool_call_id {tool_call_id!r}")
        if row.status is not RowStatus.PENDING:
            raise PendingRowError(
                f"pending row {tool_call_id!r} already filled ({row.status.value})"
            )
        self.rows[tool_call_id] = replace(
            row,
            status=status,
            result=result,
            error=error,
            finished_at=finished_at,
        )

    def is_empty(self) -> bool:
        return not self.rows

    def is_complete(self) -> bool:
        return bool(self.rows) and all(
            row.status is not RowStatus.PENDING for row in self.rows.values()
        )

    def ordered_results(self) -> list[dict[str, str]]:
        """Tool-result messages in original tool_calls order (by ``order``)."""
        return [
            {"role": "tool", "tool_call_id": row.tool_call_id, "content": row.result or ""}
            for row in sorted(self.rows.values(), key=lambda r: r.order)
        ]

    def find_by_ref(self, ref: int | str) -> PendingRow | None:
        for row in self.rows.values():
            if row.ref == ref:
                return row
        return None

    def pending_refs(self) -> list[int | str]:
        return [
            row.ref
            for row in self.rows.values()
            if row.status is RowStatus.PENDING and row.ref is not None
        ]

    def clear(self) -> None:
        self.rows.clear()


__all__ = [
    "PendingEventTable",
    "PendingRow",
    "PendingRowError",
    "RowKind",
    "RowStatus",
]
