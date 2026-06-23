from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from opencollab.application.event_bus import EventBus
from opencollab.application.session_run import SessionRunUseCase
from opencollab.domain.pending import RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


def llm_response(
    content=None,
    tool_calls=None,
    total_tokens=5,
    input_tokens=1,
    finish_reason="stop",
    reasoning=None,
):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(total_tokens=total_tokens, input_tokens=input_tokens),
        finish_reason=finish_reason,
        reasoning=reasoning,
    )


def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeAgent:
    model = "fake-model"
    temperature = 0.35

    def __init__(self, schemas=None):
        self._schemas = schemas if schemas is not None else []

    def tool_schemas(self):
        return copy.deepcopy(self._schemas)


class FakeLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)


class FakeToolExecution:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ToolProcessingResult()

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        return self.result


class FakeToolExecutionDeferred:
    """Tool executor that also handles deferrable tools via ``execute_deferred``.

    ``deferred_outcomes`` maps a tool_call_id -> (ref, error): a non-None ref
    means the tool deferred work (returns a child aid to await); a non-None
    error means it resolved synchronously and fills its row at once.
    """

    def __init__(self, process_result=None, deferred_outcomes=None):
        self.process_calls = []
        self.deferred_calls = []
        self.process_result = process_result if process_result is not None else ToolProcessingResult()
        self.deferred_outcomes = deferred_outcomes or {}

    async def process(self, tool_calls):
        self.process_calls.append(copy.deepcopy(tool_calls))
        return self.process_result

    async def execute_deferred(self, tc):
        self.deferred_calls.append(copy.deepcopy(tc))
        return self.deferred_outcomes.get(tc["id"], (None, "no outcome"))


class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens=0, latency=0.0):
        self.steps.append(
            {
                "step_type": step_type,
                "payload": copy.deepcopy(payload),
                "tokens": tokens,
                "latency": latency,
            }
        )

    def flush(self):
        self.flush_count += 1


def collect_events():
    events = []

    async def sink(event):
        events.append((event.type, copy.deepcopy(event.data)))

    return events, EventBus(sink)


def build_runner(
    *,
    state=None,
    agent=None,
    llm=None,
    event_bus=None,
    tool_execution=None,
    tracer=None,
    **kwargs,
):
    state = state if state is not None else SessionState(messages=[{"role": "system", "content": "sys"}])
    return SessionRunUseCase(
        agent=agent if agent is not None else FakeAgent(),
        state=state,
        llm=llm if llm is not None else FakeLLM([llm_response(content="done")]),
        event_publisher=event_bus if event_bus is not None else EventBus(None),
        tool_execution=tool_execution if tool_execution is not None else FakeToolExecution(),
        tracer=tracer,
        **kwargs,
    )


def test_run_loop_budget_exceeded_emits_error_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=10)
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus, max_budget_tokens=10)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.BUDGET_EXCEEDED
    assert state.terminal_reason == "budget exceeded: 10 tokens used"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Budget exceeded: 10 tokens used. Session stopped.]",
    }
    assert events == [("error", {"reason": "budget_exceeded", "aid": -1})]


def test_run_loop_team_aggregate_ceiling_stops_under_own_cap():
    # Per-session cap is generous (1_000_000) and the session has spent nothing,
    # so the per-session check passes — but the injected team-aggregate predicate
    # reports the global pool exhausted, so the session must still stop.
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=0)
    llm = FakeLLM()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        max_budget_tokens=1_000_000,
        team_budget_exhausted=lambda: True,
    )

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []  # never called the model
    assert state.phase is SessionPhase.BUDGET_EXCEEDED
    assert "team budget exceeded" in state.terminal_reason
    assert events == [("error", {"reason": "budget_exceeded", "aid": -1})]


def test_run_loop_team_aggregate_ceiling_not_reached_proceeds_normally():
    # When the aggregate predicate reports headroom, the per-session loop runs as
    # before (regression guard that the new check is additive, not a hard stop).
    events, bus = collect_events()
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(
        llm=llm,
        event_bus=bus,
        max_budget_tokens=1_000_000,
        team_budget_exhausted=lambda: False,
    )

    result = run(runner.run_loop())

    assert result == "done"
    assert len(llm.calls) == 1


def test_run_loop_step_limit_exceeded_emits_error_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], step_count=3)
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus, max_steps=3)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STEP_LIMIT_EXCEEDED
    assert state.terminal_reason == "step limit reached: 3 steps"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Step limit reached: 3 steps. Session stopped.]",
    }
    assert events == [("error", {"reason": "step_limit_exceeded", "aid": -1})]


def test_run_loop_cancel_event_appends_interrupt_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus)
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = run(runner.run_loop(cancel_event=cancel_event))

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.CANCELLED
    assert state.messages[-1] == {"role": "system", "content": "[Session interrupted by user]"}
    assert events == [("error", {"reason": "cancelled", "aid": -1})]


def test_run_loop_llm_step_events_trace_and_message_shape():
    events, bus = collect_events()
    schemas = [{"type": "function", "function": {"name": "fake_tool"}}]
    tool_calls = [tool_call()]
    llm = FakeLLM(
        [
            llm_response(content="need tool", tool_calls=tool_calls, total_tokens=9, finish_reason="tool_calls"),
            llm_response(content="done", total_tokens=4),
        ]
    )
    tracer = FakeTracer()
    tool_execution = FakeToolExecution(
        ToolProcessingResult(
            messages_to_append=[{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]
        )
    )
    runner = build_runner(
        agent=FakeAgent(schemas),
        llm=llm,
        event_bus=bus,
        tool_execution=tool_execution,
        tracer=tracer,
    )

    result = run(runner.run_loop())

    assert result == "done"
    assert llm.calls[0]["tools"] == schemas
    assert llm.calls[0]["temperature"] == 0.35
    assert tool_execution.calls == [tool_calls]
    assert [event_type for event_type, _data in events] == [
        "step_start",
        "text_delta",
        "step_end",
        "step_start",
        "text_delta",
        "step_end",
    ]
    assert events[0] == ("step_start", {"step": 1, "aid": -1})
    assert events[2][1]["step"] == 1
    assert events[2][1]["latency"] == tracer.steps[0]["latency"]
    assert tracer.steps[0] == {
        "step_type": "llm_call",
        "payload": {
            "model": "fake-model",
            "finish_reason": "tool_calls",
            "content": "need tool",
            "tool_calls": [{"id": "call-1", "name": "fake_tool", "arguments": '{"value": 1}'}],
        },
        "tokens": 9,
        "latency": tracer.steps[0]["latency"],
    }


def test_run_loop_without_tool_calls_marks_done():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    runner = build_runner(
        state=state,
        llm=FakeLLM([llm_response(content="plain", total_tokens=6)]),
        event_bus=bus,
    )

    result = run(runner.run_loop())

    assert result == "plain"
    assert state.is_done is True
    assert state.phase is SessionPhase.DONE
    assert state.used_tokens == 6
    assert state.messages[-1] == {"role": "assistant", "content": "plain"}
    assert [event_type for event_type, _data in events] == ["step_start", "text_delta", "step_end"]


# ---------------------------------------------------------------------------
# Empty-stop retry — a finish_reason="stop" turn with no text and no tool calls
# ---------------------------------------------------------------------------


def _no_consecutive_same_role(messages):
    """No two adjacent messages share a role (the role-alternation contract)."""
    return all(a["role"] != b["role"] for a, b in zip(messages, messages[1:]))


def _convo():
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]


def test_empty_stop_retries_once_with_nudge_then_succeeds():
    from opencollab.application.session_run import _EMPTY_STOP_NUDGE, _EMPTY_STOP_PLACEHOLDER

    state = SessionState(messages=_convo())
    llm = FakeLLM(
        [
            llm_response(content=None),  # empty-stop
            llm_response(content="recovered", total_tokens=4),
        ]
    )
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == "recovered"
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 2  # initial empty turn + one retry
    # The retry call saw the nudge as a user message...
    assert {"role": "user", "content": _EMPTY_STOP_NUDGE} in llm.calls[1]["messages"]
    # ...preceded by an assistant placeholder so roles still alternate (no two
    # consecutive user messages, which Anthropic rejects).
    assert _no_consecutive_same_role(state.messages)
    assert {"role": "assistant", "content": _EMPTY_STOP_PLACEHOLDER} in state.messages
    # No bare empty assistant message was ever appended.
    assert all(
        not (m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls"))
        for m in state.messages
    )
    assert state.messages[-1] == {"role": "assistant", "content": "recovered"}


def test_empty_stop_retries_at_most_once_then_gives_up():
    # Two consecutive empty-stops: retry fires once, then the session finishes
    # cleanly rather than looping. A 3rd LLM call would make FakeLLM raise.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None), llm_response(content=None)])
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == ""  # placeholder is filtered out, never returned as an answer
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 2  # bounded: initial + exactly one retry


def test_empty_stop_retry_is_recorded_in_trajectory():
    # The retry must leave a grep-able trace entry, since the injected
    # nudge/placeholder messages are never persisted to the trajectory.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None), llm_response(content="ok", total_tokens=4)])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    retries = [s for s in tracer.steps if s["step_type"] == "empty_stop_retry"]
    assert len(retries) == 1
    assert retries[0]["payload"] == {"finish_reason": "stop", "had_reasoning": False}


def test_llm_trace_records_reasoning_when_present():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer", reasoning="step-by-step thoughts")])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["reasoning"] == "step-by-step thoughts"


def test_llm_trace_omits_reasoning_when_absent():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer")])  # reasoning defaults to None
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert "reasoning" not in llm_calls[0]["payload"]


def test_empty_stop_with_length_finish_reason_does_not_retry():
    # A truncated turn (finish_reason="length") with empty content is NOT an
    # empty-stop: a nudge cannot un-truncate it, so we must not waste a retry.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=None, finish_reason="length")])
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == ""
    assert state.phase is SessionPhase.DONE
    assert len(llm.calls) == 1  # no retry attempted


def test_empty_stop_retry_budget_resets_across_turns():
    # The once-per-turn flag must reset on a new turn, or a long-lived session
    # that hit an empty-stop in turn 1 would never retry again.
    state = SessionState(messages=_convo())
    llm = FakeLLM(
        [
            llm_response(content=None),  # turn 1 empty-stop
            llm_response(content="answer-1", total_tokens=4),
            llm_response(content=None),  # turn 2 empty-stop
            llm_response(content="answer-2", total_tokens=4),
        ]
    )
    runner = build_runner(state=state, llm=llm)

    assert run(runner.run_loop()) == "answer-1"
    assert len(llm.calls) == 2

    # Start a fresh user turn on the SAME runner instance.
    state.reset_for_user_turn()
    state.append_message({"role": "user", "content": "follow-up"})
    assert run(runner.run_loop()) == "answer-2"
    assert len(llm.calls) == 4  # turn 2 retried too — flag was reset


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


# ---------------------------------------------------------------------------
# Context-overflow safety net
# ---------------------------------------------------------------------------


class FakeOverflowError(Exception):
    """A context-overflow provider rejection stand-in (no real SDK / network)."""


def _is_overflow(exc):
    return isinstance(exc, FakeOverflowError)


class OverflowThenOkLLM:
    """Raises a context overflow on the FIRST ``complete`` call, then succeeds.

    Records the messages it was handed each call so a test can assert the
    retried (forced-compaction) prompt is smaller than the first attempt.
    """

    def __init__(self, ok_response):
        self.ok_response = ok_response
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        if len(self.calls) == 1:
            raise FakeOverflowError("prompt is too long")
        return self.ok_response


class AlwaysOverflowLLM:
    """Raises a context overflow on EVERY ``complete`` call."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        raise FakeOverflowError("prompt is too long")


class FakeForcedShaper:
    """A shaper that no-ops on the normal pass but compacts hard when forced.

    Mirrors the real reactive layers' ``_forced`` contract: ``shape`` returns
    the messages unchanged until ``forced_shape`` flips ``_forced`` on, at which
    point it drops all but the first (pinned) message — standing in for a
    maximal compaction pass.
    """

    def __init__(self):
        self._forced = False

    def shape(self, messages):
        if not self._forced:
            return list(messages)
        return messages[:1]


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
    assert state.phase is SessionPhase.CONTEXT_OVERFLOW
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
    assert "context_overflow" in reasons
    # The provider was tried exactly twice (initial + one forced retry).
    assert len(llm.calls) == 2


def test_overflow_classifier_off_propagates_as_error():
    # Without an overflow classifier wired (standalone/legacy), an overflow is
    # an opaque exception: the run loop fails to ERROR and re-raises, exactly as
    # before the safety net existed (regression guard that the net is additive).
    import pytest

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = AlwaysOverflowLLM()
    runner = build_runner(state=state, llm=llm, shaper=FakeForcedShaper())

    with pytest.raises(FakeOverflowError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    # Only one call: with no classifier it isn't recognised, so no retry.
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Per-call generation timeout (P7): one slow generation can't eat the whole wall
# ---------------------------------------------------------------------------


class SlowLLM:
    """A provider whose ``complete`` awaits ``delay`` seconds before answering.

    Stands in for a death-slow thinking generation; with a small
    ``per_call_timeout`` the run loop must surface ``asyncio.TimeoutError``.
    """

    def __init__(self, response, delay):
        self.response = response
        self.delay = delay
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        await asyncio.sleep(self.delay)
        return self.response


def test_per_call_timeout_raises_on_slow_generation():
    import pytest

    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = SlowLLM(llm_response(content="too late"), delay=1.0)
    runner = build_runner(state=state, llm=llm, per_call_timeout=0.01)

    # The single generation exceeds the 0.01s ceiling -> the run loop marks the
    # session failed and re-raises the TimeoutError.
    with pytest.raises(asyncio.TimeoutError):
        run(runner.run_loop())

    assert state.phase is SessionPhase.ERROR
    assert len(llm.calls) == 1  # the call was attempted (then cancelled)


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


def test_shaper_bounds_model_view_but_leaves_state_messages_full():
    from opencollab.application.shaping import PerToolResultBudgetShaper

    big = "x" * 5000
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "tool", "tool_call_id": "t1", "content": big},
        ]
    )
    llm = FakeLLM([llm_response(content="done")])
    runner = build_runner(state=state, llm=llm, shaper=PerToolResultBudgetShaper(max_chars=1000))

    run(runner.run_loop())

    # The model saw a bounded copy...
    sent_tool = [m for m in llm.calls[0]["messages"] if m.get("role") == "tool"][0]
    assert len(sent_tool["content"]) <= 1000
    assert "re-read a narrower range" in sent_tool["content"]
    # ...while the persisted history kept the full result.
    assert state.messages[1]["content"] == big
