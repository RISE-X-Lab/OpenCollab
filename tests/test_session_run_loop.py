"""Core session run-loop behavior and tracing tests."""

from __future__ import annotations

import asyncio

import pytest
from session_run_loop_test_support import (
    FakeAgent,
    FakeLLM,
    FakeToolExecution,
    FakeTracer,
    _convo,
    _no_consecutive_same_role,
    build_runner,
    collect_events,
    llm_response,
    run,
    tool_call,
)

from opencollab.domain.session import (
    SessionPhase,
    SessionState,
    TurnEnforcementState,
)
from opencollab.domain.tools import ToolProcessingResult


@pytest.mark.parametrize(
    "per_call_timeout",
    [0, -1, float("nan"), float("inf"), True, "bad"],
)
def test_runner_rejects_unbounded_per_call_timeout(per_call_timeout):
    with pytest.raises(ValueError, match="finite positive"):
        build_runner(per_call_timeout=per_call_timeout)

def test_run_loop_budget_exceeded_emits_error_and_sets_phase():
    events, bus = collect_events()
    state = SessionState(messages=[{"role": "system", "content": "sys"}], used_tokens=10)
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus, max_budget_tokens=10)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "budget exceeded: 10 tokens used"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Budget exceeded: 10 tokens used. Session stopped.]",
    }
    assert events == [("error", {"reason": "budget exceeded: 10 tokens used", "aid": -1})]

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
    assert state.phase is SessionPhase.STOPPED
    assert "team budget exceeded" in state.terminal_reason
    assert events == [
        ("error", {"reason": "team budget exceeded: aggregate spend reached the global cap", "aid": -1})
    ]

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
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "step limit reached: 3 steps"
    assert state.messages[-1] == {
        "role": "system",
        "content": "[Step limit reached: 3 steps. Session stopped.]",
    }
    assert events == [("error", {"reason": "step limit reached: 3 steps", "aid": -1})]

def test_new_turn_precheck_failure_does_not_return_previous_answer():
    state = SessionState(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "second question"},
        ],
        step_count=3,
    )
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, max_steps=3)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED

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
    assert state.phase is SessionPhase.STOPPED
    assert state.messages[-1] == {"role": "system", "content": "[Session interrupted by user]"}
    assert events == [("error", {"reason": "interrupted by user", "aid": -1})]

def test_run_loop_loop_block_limit_stops_before_next_llm_call():
    events, bus = collect_events()
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        turn=TurnEnforcementState(loop_blocked_since_progress=3),
    )
    llm = FakeLLM()
    runner = build_runner(state=state, llm=llm, event_bus=bus)

    result = run(runner.run_loop())

    assert result == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "loop block limit reached: 3 repeated tool calls"
    assert events == [("error", {"reason": "loop block limit reached: 3 repeated tool calls", "aid": -1})]

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
            "thinking": False,
            "finish_reason": "tool_calls",
            "content": "need tool",
            "tool_calls": [{"id": "call-1", "name": "fake_tool", "arguments": '{"value": 1}'}],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 9,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "uncached_input_tokens": 1,
                "estimated": False,
            },
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
    # The retry call saw the nudge as a user message. Because that retry turn's
    # shaped history ends with the user nudge, the per-turn status block folds INTO
    # it (rather than appending a second consecutive user turn), so the nudge text
    # rides in a user message alongside the status rather than as a standalone dict.
    retry_sent = llm.calls[1]["messages"]
    assert any(
        m["role"] == "user" and _EMPTY_STOP_NUDGE in (m.get("content") or "")
        for m in retry_sent
    )
    assert _no_consecutive_same_role(retry_sent)
    # The nudge is persisted to state.messages, preceded by an assistant
    # placeholder so roles still alternate (no two consecutive user messages,
    # which Anthropic rejects). The retry's per-turn budget block folds into that
    # same user turn, so the nudge now leads a user message rather than standing
    # alone as a bare dict.
    assert any(
        m["role"] == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(_EMPTY_STOP_NUDGE)
        for m in state.messages
    )
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

def test_llm_trace_records_effective_thinking_mode():
    agent = FakeAgent()
    agent.thinking = True
    tracer = FakeTracer()
    runner = build_runner(agent=agent, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["thinking"] is True

def test_reasoning_is_preserved_in_assistant_tool_call_history():
    state = SessionState(messages=_convo())
    response = llm_response(
        tool_calls=[tool_call()],
        reasoning="I need to inspect the file first.",
    )
    runner = build_runner(state=state)

    runner.append_assistant_message(response)

    assert state.messages[-1]["reasoning_content"] == "I need to inspect the file first."
    assert state.messages[-1]["tool_calls"] == response.tool_calls

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
