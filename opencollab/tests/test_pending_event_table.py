from __future__ import annotations

import pytest
from opencollab.domain.pending import (
    PendingEventTable,
    PendingRow,
    PendingRowError,
    RowKind,
    RowStatus,
)


def child_row(tool_call_id: str, order: int, ref: int) -> PendingRow:
    return PendingRow(
        tool_call_id=tool_call_id,
        kind=RowKind.CHILD_AGENT,
        order=order,
        ref=ref,
    )


def test_empty_table_is_empty_and_not_complete():
    t = PendingEventTable()
    assert t.is_empty()
    assert not t.is_complete()


def test_add_then_fill_completes():
    t = PendingEventTable()
    t.add(child_row("c1", order=0, ref=5))
    assert not t.is_empty()
    assert not t.is_complete()
    t.fill("c1", result="done")
    assert t.is_complete()


def test_duplicate_add_raises():
    t = PendingEventTable()
    t.add(child_row("c1", order=0, ref=5))
    with pytest.raises(PendingRowError):
        t.add(child_row("c1", order=1, ref=6))


def test_fill_unknown_id_raises():
    t = PendingEventTable()
    with pytest.raises(PendingRowError):
        t.fill("nope", result="x")


def test_double_fill_raises():
    t = PendingEventTable()
    t.add(child_row("c1", order=0, ref=5))
    t.fill("c1", result="done")
    with pytest.raises(PendingRowError):
        t.fill("c1", result="again")


def test_ordered_results_sorted_by_order():
    t = PendingEventTable()
    t.add(child_row("c2", order=1, ref=2))
    t.add(child_row("c1", order=0, ref=1))
    t.fill("c2", result="second")
    t.fill("c1", result="first")
    assert t.ordered_results() == [
        {"role": "tool", "tool_call_id": "c1", "content": "first"},
        {"role": "tool", "tool_call_id": "c2", "content": "second"},
    ]


def test_mixed_batch_incomplete_until_all_filled():
    t = PendingEventTable()
    t.add(
        PendingRow(tool_call_id="bash", kind=RowKind.IMMEDIATE, order=0, status=RowStatus.DONE, result="ok")
    )
    t.add(child_row("spawn", order=1, ref=7))
    assert not t.is_complete()  # deferred row still pending
    t.fill("spawn", result="child result")
    assert t.is_complete()


def test_failed_fill_surfaces_result_text():
    t = PendingEventTable()
    t.add(child_row("c1", order=0, ref=5))
    t.fill("c1", result="Error: boom", status=RowStatus.FAILED, error="boom")
    assert t.is_complete()
    assert t.ordered_results()[0]["content"] == "Error: boom"


def test_clear_empties_table():
    t = PendingEventTable()
    t.add(child_row("c1", order=0, ref=5))
    t.clear()
    assert t.is_empty()
