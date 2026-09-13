"""The workflow budget pool must leave a record when it refuses or waves a call through.

``_acquire_budget_lease`` is the pre-call gate on the shared token pool, and it
has two decision points that used to leave nothing behind. It raises
``WorkflowBudgetExceeded`` when the pool is spent, and — when the caller passed
``over_budget_ok=True`` — it instead waves the call through on an exhausted
pool. A finished run showed only ``reason="budget_exceeded"`` in
``workflow.json``: never which call was refused, at what point in the run, or by
how much it overshot, and the escape was invisible altogether.

These tests pin two structured trace records, ``budget_refusal`` and
``budget_escape``. They drive the real :class:`~opencollab.adapters.trace.Tracer`
and read the JSONL back off disk, so a field that never reaches the file fails
here.

They also pin the ENFORCEMENT around each record — the refusal still raises and
still builds no session, the escape still runs the call with the cap it asked
for. This change adds observation only; a test that proved the record landed but
not that behaviour stayed put would let a semantics change ride along unnoticed.
"""

from __future__ import annotations

import json

import pytest
from workflow_context_test_support import FakeFactory, FakeSession

from opencollab.adapters.trace import Tracer
from opencollab.application.workflow import (
    UNBOUNDED_SESSION_BUDGET,
    WorkflowBudgetExceeded,
    WorkflowContext,
)


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


@pytest.mark.asyncio
async def test_a_refused_call_records_who_was_refused_and_by_how_much(tmp_path):
    tracer = Tracer(run_id="budget-refusal", output_dir=str(tmp_path))
    # One session overspends the pool outright: 150 tokens against a total of
    # 100, so the next call meets a pool that is 50 tokens in the hole.
    factory = FakeFactory([FakeSession(reply="a", tokens=150)])
    ctx = WorkflowContext(factory, tracer=tracer, budget_total=100)
    try:
        assert await ctx.agent("first", label="scout") == "a"
        assert ctx.budget.spent() == 150
        assert ctx.budget.remaining() == -50

        with pytest.raises(WorkflowBudgetExceeded):
            await ctx.agent("second", label="coder", budget=40)
        tracer.flush()
    finally:
        tracer.close()

    # Enforcement unchanged: the refusal still raised, and still built nothing.
    assert len(factory.builds) == 1

    payloads = _payloads(tracer.path, "budget_refusal")
    assert len(payloads) == 1
    assert payloads[0] == {
        "seq": 1,
        "agent_id": None,
        "label": "coder",
        "requested_cap": 40,
        "remaining": -50,
        "spent": 150,
        "total": 100,
        "would_exceed_by": 90,
    }
    # The healthy first call is not a budget decision and must not be recorded.
    assert _payloads(tracer.path, "budget_escape") == []


@pytest.mark.asyncio
async def test_an_over_budget_escape_records_the_call_it_waved_through(tmp_path):
    tracer = Tracer(run_id="budget-escape", output_dir=str(tmp_path))
    factory = FakeFactory(
        [FakeSession(reply="a", tokens=150), FakeSession(reply="forced write", tokens=0)]
    )
    ctx = WorkflowContext(factory, tracer=tracer, budget_total=100)
    try:
        assert await ctx.agent("first", label="scout") == "a"
        result = await ctx.agent(
            "final write", label="closer", budget=25, over_budget_ok=True
        )
        tracer.flush()
    finally:
        tracer.close()

    # Enforcement unchanged: the escape still ran the call, with the cap it asked
    # for, on a pool that was already 50 tokens overdrawn.
    assert result == "forced write"
    assert len(factory.builds) == 2
    assert factory.builds[1]["budget"] == 25

    payloads = _payloads(tracer.path, "budget_escape")
    assert len(payloads) == 1
    assert payloads[0] == {
        "seq": 1,
        "agent_id": None,
        "label": "closer",
        "requested_cap": 25,
        "remaining": -50,
        "spent": 150,
        "total": 100,
        "would_exceed_by": 75,
        "over_budget_ok": True,
    }
    assert _payloads(tracer.path, "budget_refusal") == []


@pytest.mark.asyncio
async def test_an_uncapped_escape_records_a_null_overshoot_not_an_invented_number(
    tmp_path,
):
    """No cap means no requested amount, so the overshoot is unknowable.

    A number here would be a guess, and this record is evidence. The escape
    still grants such a call ``UNBOUNDED_SESSION_BUDGET``, which the build
    assertion pins so the record cannot be read as a smaller grant.
    """
    tracer = Tracer(run_id="budget-escape-uncapped", output_dir=str(tmp_path))
    factory = FakeFactory(
        [FakeSession(reply="a", tokens=150), FakeSession(reply="forced write", tokens=0)]
    )
    ctx = WorkflowContext(factory, tracer=tracer, budget_total=100)
    try:
        assert await ctx.agent("first", label="scout") == "a"
        result = await ctx.agent("final write", over_budget_ok=True)
        tracer.flush()
    finally:
        tracer.close()

    assert result == "forced write"
    assert factory.builds[1]["budget"] == UNBOUNDED_SESSION_BUDGET

    payloads = _payloads(tracer.path, "budget_escape")
    assert len(payloads) == 1
    assert payloads[0] == {
        "seq": 1,
        # No label was passed, so there is nothing that names the caller.
        "agent_id": None,
        "label": None,
        "requested_cap": None,
        "remaining": -50,
        "spent": 150,
        "total": 100,
        "would_exceed_by": None,
        "over_budget_ok": True,
    }
