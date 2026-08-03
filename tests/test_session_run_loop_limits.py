"""Session run-loop context overflow and timeout tests."""

from __future__ import annotations

import asyncio

import pytest
from session_run_loop_test_support import (
    AlwaysOverflowLLM,
    CancelCleanupLLM,
    FakeForcedShaper,
    FakeOverflowError,
    OverflowThenOkLLM,
    SlowLLM,
    _agent_with_tool_schemas,
    _is_overflow,
    build_runner,
    collect_events,
    llm_response,
    run,
)

from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.application.session_run import GenerationTimeoutError
from opencollab.application.shaping import OldHistorySnipShaper, ShaperPipeline
from opencollab.domain.session import (
    SessionPhase,
    SessionState,
)


def test_call_llm_recompacts_and_retries_once_on_overflow():
    # A long history; the shaper no-ops normally (so the first call overflows),
    # then a forced compaction shrinks it and the retry succeeds.
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a" * 4000},
            {"role": "assistant", "content": "b" * 4000},
        ]
    )
    llm = OverflowThenOkLLM(llm_response(content="recovered"))
    runner = build_runner(
        state=state,
        llm=llm,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    result = run(runner.run_loop())

    assert result == "recovered"
    assert state.phase is SessionPhase.DONE
    # Two provider calls: the overflowing first, the forced-compacted retry.
    assert len(llm.calls) == 2
    # The retry's prompt is strictly smaller (forced compaction ran).
    assert len(llm.calls[1]) < len(llm.calls[0])
    assert llm.calls[1] == state.messages[:1]
    # The original history is untouched by shaping (read-time only); only the
    # new assistant answer was appended on success.
    assert state.messages[:3] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a" * 4000},
        {"role": "assistant", "content": "b" * 4000},
    ]
    assert state.messages[-1] == {"role": "assistant", "content": "recovered"}


def test_relay_wire_limit_recompacts_and_retries_once():
    class RelayWireOverflow(Exception):
        status_code = 413
        body = {"error": {"code": "upstream_request_too_large"}}

    class RelayOverflowThenOk:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, tools=None, temperature=0.0):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                raise RelayWireOverflow("encoded request exceeds wire byte limit")
            return llm_response(content="recovered")

    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "large history"},
        ]
    )
    llm = RelayOverflowThenOk()
    runner = build_runner(
        state=state,
        llm=llm,
        shaper=FakeForcedShaper(),
        is_context_overflow=is_context_overflow_error,
    )

    assert run(runner.run_loop()) == "recovered"
    assert len(llm.calls) == 2
    assert llm.calls[1] == state.messages[:1]


def test_overflow_retry_preserves_hard_write_gate():
    class OverflowThenWrite:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            self.calls.append({"messages": messages, "tools": tools, **kwargs})
            if len(self.calls) == 1:
                raise FakeOverflowError("prompt is too long")
            return llm_response(content="recovered")

    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
    )
    state.turn.reads_since_last_edit = 12
    llm = OverflowThenWrite()
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("file_read", "file_write", "apply_patch"),
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    assert run(runner.run_loop()) == "recovered"
    assert len(llm.calls) == 2
    assert [spec["function"]["name"] for spec in llm.calls[1]["tools"]] == [
        "file_write",
        "apply_patch",
    ]
    assert llm.calls[1]["tool_choice"] == "required"


def test_overflow_retry_preserves_failed_edit_error_and_recovery_gate():
    class OverflowThenRecover:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            self.calls.append({"messages": messages, "tools": tools, **kwargs})
            if len(self.calls) == 1:
                raise FakeOverflowError("prompt is too long")
            return llm_response(content="recovered")

    target = "pkg/module.py"
    large_arguments = '{"path":"pkg/module.py","mode":"unified_diff","patch":"' + "x" * 2000 + '"}'
    error = f"Error applying patch to {target}: hunk did not match"
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "old-read",
                    "function": {"name": "file_read", "arguments": '{}'},
                }],
            },
            {"role": "tool", "tool_call_id": "old-read", "content": "old source"},
            {
                "role": "assistant",
                "reasoning_content": "working notes" * 200,
                "tool_calls": [{
                    "id": "failed-edit",
                    "function": {"name": "apply_patch", "arguments": large_arguments},
                }],
            },
            {"role": "tool", "tool_call_id": "failed-edit", "content": error},
        ],
    )
    state.turn.reads_since_last_edit = 12
    llm = OverflowThenRecover()
    runner = build_runner(
        state=state,
        llm=llm,
        agent=_agent_with_tool_schemas("file_read", "grep", "file_write", "apply_patch"),
        shaper=ShaperPipeline((OldHistorySnipShaper(
            trigger_tokens=1_000_000,
            target_tokens=1,
            keep_recent_groups=1,
        ),)),
        is_context_overflow=_is_overflow,
    )

    assert run(runner.run_loop()) == "recovered"
    retry = llm.calls[1]
    assert any(message.get("content") == error for message in retry["messages"])
    assert any(target in str(message.get("content", "")) for message in retry["messages"])
    failed_call = next(
        call for message in retry["messages"] for call in message.get("tool_calls", [])
        if call.get("id") == "failed-edit"
    )
    compacted = failed_call["function"]["arguments"]
    assert "completed edit arguments" in compacted
    assert "x" * 100 not in compacted
    assert [spec["function"]["name"] for spec in retry["tools"]] == ["file_read", "grep"]
    assert retry["tool_choice"] == "required"

def test_call_llm_emits_recompaction_event_on_overflow():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = OverflowThenOkLLM(llm_response(content="ok"))
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    run(runner.run_loop())

    reasons = [data["reason"] for etype, data in events if etype == "error"]
    assert "context_overflow_recompacted" in reasons

def test_persistent_overflow_stops_gracefully_not_unhandled():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = AlwaysOverflowLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        shaper=FakeForcedShaper(),
        is_context_overflow=_is_overflow,
    )

    # No unhandled exception — the loop returns normally with a controlled stop.
    result = run(runner.run_loop())

    assert result == ""
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("context overflow")
    assert state.messages[-1] == {
        "role": "system",
        "content": (
            "[Context overflow: prompt exceeds the model context window "
            "even after compaction. Session stopped.]"
        ),
    }
    # Both the recompaction notice and the final overflow error were emitted.
    reasons = [data["reason"] for etype, data in events if etype == "error"]
    assert "context_overflow_recompacted" in reasons
    assert "context overflow: prompt exceeds the model context window even after compaction" in reasons
    # The provider was tried exactly twice (initial + one forced retry).
    assert len(llm.calls) == 2

def test_overflow_classifier_off_propagates_as_error():
    # Without an overflow classifier wired (standalone/legacy), an overflow is
    # an opaque exception: the run loop fails to ERROR and re-raises, exactly as
    # before the safety net existed (regression guard that the net is additive).

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = AlwaysOverflowLLM()
    runner = build_runner(state=state, llm=llm, shaper=FakeForcedShaper())

    with pytest.raises(FakeOverflowError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    # Only one call: with no classifier it isn't recognised, so no retry.
    assert len(llm.calls) == 1

def test_per_call_timeout_raises_on_slow_generation():

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = SlowLLM(llm_response(content="too late"), delay=1.0)
    runner = build_runner(state=state, llm=llm, per_call_timeout=0.01)

    # The single generation exceeds the 0.01s ceiling -> the run loop marks the
    # session failed and re-raises the generation-specific timeout.
    with pytest.raises(GenerationTimeoutError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    assert len(llm.calls) == 1  # the call was attempted (then cancelled)

def test_per_call_timeout_returns_before_cancel_cleanup_finishes():
    async def scenario():
        state = SessionState(messages=[{"role": "system", "content": "sys"}])
        llm = CancelCleanupLLM()
        runner = build_runner(state=state, llm=llm, per_call_timeout=0.01)

        raised = False
        try:
            await asyncio.wait_for(runner.run_loop(), timeout=0.5)
        except GenerationTimeoutError:
            raised = True

        assert raised is True
        assert state.phase is SessionPhase.ERROR
        assert "GenerationTimeoutError" in (state.terminal_reason or "")
        assert len(runner.pending_cleanup_tasks) == 1
        llm.release_cancel.set()
        while runner.pending_cleanup_tasks:
            await asyncio.sleep(0)
        assert llm.cancel_seen.is_set()

    run(scenario())

def test_provider_timeout_is_not_relabelled_as_generation_ceiling():

    class ProviderTimeoutLLM:
        async def complete(self, messages, tools=None, temperature=0.0):
            raise asyncio.TimeoutError("provider transport timeout")

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(
        state=state,
        llm=ProviderTimeoutLLM(),
        per_call_timeout=10.0,
    )

    with pytest.raises(asyncio.TimeoutError) as captured:
        run(runner.run_loop())

    assert not isinstance(captured.value, GenerationTimeoutError)
    assert "provider transport timeout" in str(captured.value)

def test_per_call_timeout_none_does_not_bound_the_call():
    # With no ceiling wired (the default), a generation that takes longer than
    # any small timeout still completes — regression guard that the ceiling is
    # additive and off by default.
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = SlowLLM(llm_response(content="finished", total_tokens=4), delay=0.05)
    runner = build_runner(state=state, llm=llm)  # per_call_timeout defaults to None

    result = run(runner.run_loop())

    assert result == "finished"
    assert state.phase is SessionPhase.DONE
