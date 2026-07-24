"""Deferred session run-loop tool execution tests."""

from __future__ import annotations

import asyncio

from session_run_loop_test_support import (
    CompletingBeforeReturnToolExecution,
    FakeLLM,
    FakeToolExecutionDeferred,
    build_runner,
    llm_response,
    run,
    tool_call,
)

from opencollab.application.session_run import (
    PendingStep,
)
from opencollab.domain.pending import RowKind, RowStatus
from opencollab.domain.session import (
    SessionPhase,
    SessionState,
)
from opencollab.domain.tools import ToolProcessingResult


def test_deferred_spawn_suspends_then_resumes_after_fill():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="spawning",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="done after child", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (7, None)})
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    # First run: a deferred spawn suspends the session on AWAITING_EVENTS.
    result = run(runner.run_loop())
    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert result == "spawning"
    assert len(llm.calls) == 1
    row = state.pending_events.rows["s1"]
    assert row.tool_call_id == "s1"
    assert row.ref == 7
    assert row.kind is RowKind.CHILD_AGENT
    assert row.status is RowStatus.PENDING

    # The scheduler fills the row when the child finishes, then re-activates.
    state.pending_events.fill("s1", result="child result")
    result2 = run(runner.run_loop())

    assert result2 == "done after child"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    # The resumed LLM call saw the child result as a proper tool result.
    second_call_msgs = llm.calls[1]["messages"]
    assert {"role": "tool", "tool_call_id": "s1", "content": "child result"} in second_call_msgs

def test_deferred_row_exists_before_fast_child_can_complete():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="spawning",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="done after fast child", total_tokens=4),
        ]
    )
    te = CompletingBeforeReturnToolExecution(state)
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    result = run(asyncio.wait_for(runner.run_loop(), timeout=0.5))

    assert result == "done after fast child"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    assert {
        "role": "tool",
        "tool_call_id": "s1",
        "content": "fast child result",
    } in llm.calls[1]["messages"]

def test_mixed_batch_buffers_immediate_and_resumes_in_order():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    batch = [
        tool_call(call_id="b1", name="fake_tool", arguments="{}"),
        tool_call(call_id="s1", name="spawn_agent", arguments="{}"),
    ]
    llm = FakeLLM(
        [
            llm_response(content="mixed", tool_calls=batch, finish_reason="tool_calls"),
            llm_response(content="final", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(
        process_result=ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "b1", "content": "bash ok"}]
        ),
        deferred_outcomes={"s1": (9, None)},
    )
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    run(runner.run_loop())
    assert state.phase is SessionPhase.AWAITING_EVENTS
    # process() ran only the immediate tool; the deferred one bypassed it.
    assert te.process_calls == [[batch[0]]]
    assert len(state.pending_events.rows) == 2
    assert not state.pending_events.is_complete()  # deferred row still pending

    state.pending_events.fill("s1", result="child done")
    run(runner.run_loop())

    # Tool-result block is contiguous and in original tool_calls order.
    second_call_msgs = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert tool_msgs == [
        {"role": "tool", "tool_call_id": "b1", "content": "bash ok"},
        {"role": "tool", "tool_call_id": "s1", "content": "child done"},
    ]

def test_deferred_batch_with_blocked_tool_uses_original_order():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    state.phase = SessionPhase.EXECUTING_TOOLS
    blocked_read = tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}')
    deferred_spawn = tool_call(call_id="s1", name="spawn_agent", arguments="{}")
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (9, None)})
    runner = build_runner(state=state, tool_execution=te)
    runner._pending = PendingStep(
        response=llm_response(
            content="mixed",
            tool_calls=[blocked_read, deferred_spawn],
            finish_reason="tool_calls",
        ),
        latency=0.0,
    )
    runner._pending_tool_allowlist = frozenset({"spawn_agent"})
    runner._pending_tool_gate_label = "test gate"

    run(runner.execute_pending_tools())

    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert state.pending_events.rows["r1"].order == 0
    assert state.pending_events.rows["s1"].order == 1
    assert "not allowed during the test gate" in state.pending_events.rows["r1"].result
    assert te.process_calls == []
    assert te.deferred_calls == [deferred_spawn]

def test_mixed_batch_folds_in_reads_counter_for_immediate_reads():
    # Regression: a mixed batch (immediate read + deferred spawn) buffers its
    # result messages, so the reads-without-write counter must still fold in via
    # apply_read_write_counter_to — not be lost because only apply_hashes_to ran.
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    batch = [
        tool_call(call_id="r1", name="file_read", arguments='{"path": "a.py"}'),
        tool_call(call_id="s1", name="spawn_agent", arguments="{}"),
    ]
    llm = FakeLLM(
        [llm_response(content="mixed", tool_calls=batch, finish_reason="tool_calls")]
    )
    te = FakeToolExecutionDeferred(
        process_result=ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "r1", "content": "file body"}],
            reads_executed=1,
        ),
        deferred_outcomes={"s1": (9, None)},
    )
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    run(runner.run_loop())

    assert state.phase is SessionPhase.AWAITING_EVENTS
    assert state.turn.reads_since_last_edit == 1  # immediate read counted despite buffering

def test_deferred_rejected_synchronously_does_not_suspend():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="try spawn",
                tool_calls=[tool_call(call_id="s1", name="spawn_agent", arguments="{}")],
                finish_reason="tool_calls",
            ),
            llm_response(content="handled error", total_tokens=4),
        ]
    )
    te = FakeToolExecutionDeferred(deferred_outcomes={"s1": (None, "Permission denied: nope")})
    runner = build_runner(state=state, llm=llm, tool_execution=te)

    result = run(runner.run_loop())

    # No event will ever arrive, so the session must not suspend — it drains
    # the synchronous error and finishes in one run.
    assert result == "handled error"
    assert state.phase is SessionPhase.DONE
    assert state.pending_events.is_empty()
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "s1", "content": "Permission denied: nope"}]
