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

from opencollab.application.steering import build_steering_block
from opencollab.domain.session import (
    SessionPhase,
    SessionState,
    TurnEnforcementState,
)
from opencollab.domain.token_estimation import (
    estimate_messages_tokens,
    estimate_request_message_tokens,
    estimate_request_tokens,
)
from opencollab.domain.tools import ToolProcessingResult


def _first_call_messages(messages):
    steering, _override, _level = build_steering_block(
        used_tokens=0,
        max_budget_tokens=500,
        step_count=1,
        max_steps=100,
        reads=0,
        has_write=False,
        has_structured_output=False,
        structured_override=None,
    )
    return [*messages, steering]


def _reserved_first_call_input(messages):
    return estimate_request_tokens(_first_call_messages(messages))


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


def test_remaining_budget_caps_next_model_output_request():
    messages = [{"role": "system", "content": "sys"}]
    input_tokens = _reserved_first_call_input(messages)
    state = SessionState(
        messages=messages,
        used_tokens=500 - input_tokens - 1,
    )
    llm = FakeLLM([
        llm_response(
            content="done",
            input_tokens=input_tokens,
            output_tokens=1,
            total_tokens=input_tokens + 1,
        )
    ])
    runner = build_runner(
        state=state,
        llm=llm,
        max_budget_tokens=500,
    )

    assert run(runner.run_loop()) == "done"
    assert llm.calls[0]["max_output_tokens"] == 1
    assert state.used_tokens == 500


def test_input_reservation_exhausting_budget_stops_before_model_call():
    events, bus = collect_events()
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        used_tokens=9,
    )
    llm = FakeLLM([
        llm_response(
            content="should not run",
            input_tokens=100,
            output_tokens=1,
            total_tokens=101,
        )
    ])
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        max_budget_tokens=10,
    )

    assert run(runner.run_loop()) == ""
    assert llm.calls == []
    assert state.used_tokens == 9
    assert state.step_count == 0
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("budget exhausted before model call")
    assert [event_type for event_type, _data in events] == ["error"]


def test_conservative_input_reservation_blocks_estimator_overshoot():
    messages = [{"role": "system", "content": "\u754c" * 3}]
    provider_messages = _first_call_messages(messages)
    ordinary_estimate = estimate_messages_tokens(provider_messages)
    reserved = estimate_request_tokens(provider_messages)
    reported_input_tokens = 143
    assert ordinary_estimate == 47
    assert ordinary_estimate + 1 < reported_input_tokens <= reserved

    state = SessionState(
        messages=messages,
        used_tokens=456,
    )
    llm = FakeLLM([
        llm_response(
            content="should not run",
            input_tokens=reported_input_tokens,
            output_tokens=1,
            total_tokens=reported_input_tokens + 1,
        )
    ])
    runner = build_runner(
        state=state,
        llm=llm,
        max_budget_tokens=500,
    )

    assert run(runner.run_loop()) == ""
    assert llm.calls == []
    assert state.used_tokens == 456
    assert state.step_count == 0
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("budget exhausted before model call")


def test_english_input_reservation_stays_within_twice_the_ordinary_estimate():
    """Guard the unit the reservation is denominated in.

    The reservation used to charge one token per serialized UTF-8 byte, which
    ran 3-5x the ordinary estimate on English histories and stopped sessions
    that still held most of their budget. Reserving in the same unit the
    estimator uses keeps the framing allowances as the only margin; a ratio
    creeping back toward 3x means the unit slipped again, not that the margin
    was tuned.
    """
    messages = [{"role": "system", "content": "You are a careful engineer."}]
    for turn in range(12):
        messages.append({
            "role": "user",
            "content": f"Please inspect module number {turn} and report defects.",
        })
        messages.append({
            "role": "assistant",
            "content": "",
            "provider_state": {
                "anthropic_content": [
                    {
                        "type": "thinking",
                        "thinking": (
                            f"Reading module {turn}, then checking its callers "
                            "before answering."
                        ),
                        "signature": f"sig-{turn}",
                    }
                ]
            },
            "tool_calls": [
                {
                    "id": f"call-{turn}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path": "src/module_{turn}.py"}}',
                    },
                }
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call-{turn}",
            "content": f"module {turn} body " * 60,
        })
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path."}
                    },
                    "required": ["path"],
                },
            },
        }
    ]
    assert len(messages) > 20

    ordinary_estimate = estimate_messages_tokens(messages, tools)
    reserved = estimate_request_tokens(messages, tools)
    assert reserved >= ordinary_estimate
    # Byte-per-token reservation put this ratio at 3.81; it is now ~1.6, all of
    # it the per-request/per-message/per-tool framing allowances.
    assert reserved < 2 * ordinary_estimate


def test_anthropic_provider_state_is_reserved_before_model_call():
    thinking_chars = 256
    base_messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": ""},
    ]
    messages = [
        base_messages[0],
        {
            **base_messages[1],
            "provider_state": {
                "anthropic_content": [
                    {
                        "type": "thinking",
                        "thinking": "\u754c" * thinking_chars,
                        "signature": "signed-state",
                    }
                ]
            },
        },
    ]
    baseline_reserved = estimate_request_tokens(_first_call_messages(base_messages))
    provider_state_reserved = estimate_request_tokens(_first_call_messages(messages))
    # The replayed thinking block is real Anthropic request input, so reserving
    # it must cost at least one token per CJK character the block carries.
    assert provider_state_reserved >= baseline_reserved + thinking_chars

    llm = FakeLLM([llm_response(content="should not run")])
    state = SessionState(messages=messages)
    runner = build_runner(
        state=state,
        llm=llm,
        max_budget_tokens=baseline_reserved + 1,
    )

    assert run(runner.run_loop()) == ""
    assert llm.calls == []
    assert state.step_count == 0
    assert state.phase is SessionPhase.STOPPED


def test_actual_usage_overrun_is_traced_and_response_is_discarded():
    events, bus = collect_events()
    tracer = FakeTracer()
    messages = [{"role": "system", "content": "sys"}]
    reserved_input_tokens = _reserved_first_call_input(messages)
    state = SessionState(
        messages=messages,
        used_tokens=500 - reserved_input_tokens - 1,
    )
    calls = [tool_call()]
    llm = FakeLLM([
        llm_response(
            content="discard me",
            tool_calls=calls,
            input_tokens=reserved_input_tokens + 100,
            output_tokens=1,
            total_tokens=reserved_input_tokens + 101,
            finish_reason="tool_calls",
        )
    ])
    tool_execution = FakeToolExecution()
    runner = build_runner(
        state=state,
        llm=llm,
        event_bus=bus,
        tool_execution=tool_execution,
        tracer=tracer,
        max_budget_tokens=500,
    )

    assert run(runner.run_loop()) == ""
    assert state.used_tokens == 600
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "budget exceeded after model call: 600 tokens used"
    assert [message["role"] for message in state.messages] == ["system", "system"]
    assert tool_execution.calls == []
    assert [event_type for event_type, _data in events] == [
        "step_start",
        "error",
        "step_end",
    ]
    # Closing row: how the session ended, beside both counters (see
    # ``_trace_session_terminal``).
    assert [step["step_type"] for step in tracer.steps] == [
        "context_shaping",
        "llm_call",
        "session_terminal",
    ]
    assert tracer.steps[1]["tokens"] == reserved_input_tokens + 101
    assert tracer.steps[1]["payload"]["usage"]["input_tokens"] == (
        reserved_input_tokens + 100
    )
    assert tracer.steps[1]["latency"] >= 0


def test_remaining_budget_caps_configured_per_step_output_limit():
    agent = FakeAgent()
    agent.max_tokens_per_step = 100
    messages = [{"role": "system", "content": "sys"}]
    input_tokens = _reserved_first_call_input(messages)
    state = SessionState(
        messages=messages,
        used_tokens=500 - input_tokens - 7,
    )
    llm = FakeLLM([
        llm_response(
            content="done",
            input_tokens=input_tokens,
            output_tokens=7,
            total_tokens=input_tokens + 7,
        )
    ])
    runner = build_runner(
        state=state,
        agent=agent,
        llm=llm,
        max_budget_tokens=500,
    )

    run(runner.run_loop())

    assert llm.calls[0]["max_output_tokens"] == 7


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "total_tokens"),
    [
        (-1, 2, 1),
        (1, -1, 0),
        (True, 1, 2),
        (1, False, 1),
        (1.5, 1, 2),
        (1, 1.5, 2),
        (1, 1, -1),
        (1, 1, True),
    ],
)
def test_invalid_provider_usage_does_not_change_token_counters(
    input_tokens, output_tokens, total_tokens
):
    state = SessionState(
        messages=[{"role": "system", "content": "sys"}],
        used_tokens=9,
        context_tokens=4,
    )
    llm = FakeLLM(
        [
            llm_response(
                content="bad usage",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        ]
    )
    runner = build_runner(state=state, llm=llm, max_budget_tokens=500)

    with pytest.raises(ValueError, match="usage"):
        run(runner.run_loop())

    assert state.used_tokens == 9
    assert state.context_tokens == 4


def test_inconsistent_reported_total_cannot_undercharge_usage():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    llm = FakeLLM(
        [
            llm_response(
                content="done",
                input_tokens=4,
                output_tokens=3,
                total_tokens=1,
            )
        ]
    )
    runner = build_runner(state=state, llm=llm, max_budget_tokens=500)

    assert run(runner.run_loop()) == "done"
    assert state.used_tokens == 7
    assert state.context_tokens == 4


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
    assert [step["step_type"] for step in tracer.steps] == [
        "context_shaping",
        "llm_call",
        "context_shaping",
        "llm_call",
        "session_terminal",
    ]
    assert events[2][1]["latency"] == tracer.steps[1]["latency"]
    assert tracer.steps[1] == {
        "step_type": "llm_call",
        "payload": {
                "aid": -1,
                "model": "fake-model",
                "thinking": False,
                "reasoning_effort_policy": "configured",
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
        "latency": tracer.steps[1]["latency"],
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


def test_whitespace_only_stop_uses_empty_turn_rescue():
    state = SessionState(messages=_convo())
    llm = FakeLLM(
        [
            llm_response(content=" \t\n"),
            llm_response(content="recovered", total_tokens=4),
        ]
    )
    runner = build_runner(state=state, llm=llm)

    assert run(runner.run_loop()) == "recovered"
    assert len(llm.calls) == 2
    assert not any(
        message.get("role") == "assistant"
        and message.get("content") == " \t\n"
        for message in state.messages
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


def test_llm_trace_records_reasoning_effort_policy():
    agent = FakeAgent()
    agent.reasoning_effort = None
    agent.reasoning_effort_policy = "suppressed"
    tracer = FakeTracer()
    runner = build_runner(agent=agent, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["reasoning_effort_policy"] == "suppressed"


def test_llm_trace_records_verified_provider_model():
    response = llm_response(content="answer")
    response.provider_model = "gpt-verified"
    tracer = FakeTracer()
    runner = build_runner(llm=FakeLLM([response]), tracer=tracer)

    run(runner.run_loop())

    llm_calls = [step for step in tracer.steps if step["step_type"] == "llm_call"]
    assert llm_calls[0]["payload"]["provider_model"] == "gpt-verified"


def test_reasoning_is_preserved_in_assistant_tool_call_history():
    state = SessionState(messages=_convo())
    response = llm_response(
        tool_calls=[tool_call()],
        reasoning="I need to inspect the file first.",
        provider_state={
            "anthropic_content": [
                {
                    "type": "thinking",
                    "thinking": "I need to inspect the file first.",
                    "signature": "signed-thinking",
                }
            ]
        },
    )
    runner = build_runner(state=state)

    runner.append_assistant_message(response)

    assert state.messages[-1]["reasoning_content"] == "I need to inspect the file first."
    assert state.messages[-1]["provider_state"] == response.provider_state
    assert state.messages[-1]["tool_calls"] == response.tool_calls

def test_llm_trace_flags_reasoning_the_provider_billed_but_withheld():
    # A turn that emits tool calls and no text, whose reasoning was charged for
    # and not returned, cannot be reconstructed at all afterwards -- and a
    # trajectory that simply lacks a reasoning field looks the same as one from
    # a model that never reasoned. Fifteen measured runs lost 1.08M reasoning
    # tokens this way before anyone noticed, so the absence is recorded.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer", reasoning_tokens=135)])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    payload = [s for s in tracer.steps if s["step_type"] == "llm_call"][0]["payload"]
    assert payload["reasoning_withheld"] is True
    assert "reasoning" not in payload

def test_llm_trace_does_not_flag_a_model_that_simply_did_not_reason():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer")])  # nothing billed, nothing returned
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    payload = [s for s in tracer.steps if s["step_type"] == "llm_call"][0]["payload"]
    assert "reasoning_withheld" not in payload

def test_llm_trace_does_not_flag_reasoning_that_did_come_back():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer", reasoning="thoughts", reasoning_tokens=90)])
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    payload = [s for s in tracer.steps if s["step_type"] == "llm_call"][0]["payload"]
    assert payload["reasoning"] == "thoughts"
    assert "reasoning_withheld" not in payload

def test_llm_trace_omits_reasoning_when_absent():
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content="answer")])  # reasoning defaults to None
    tracer = FakeTracer()
    runner = build_runner(state=state, llm=llm, tracer=tracer)

    run(runner.run_loop())

    llm_calls = [s for s in tracer.steps if s["step_type"] == "llm_call"]
    assert "reasoning" not in llm_calls[0]["payload"]

@pytest.mark.parametrize(
    "finish_reason", ["length", "max_tokens", "model_context_window_exceeded"]
)
@pytest.mark.parametrize("content", [None, "partial answer"])
def test_length_finish_reason_stops_with_partial_output(content, finish_reason):
    # A provider length stop is truncated regardless of whether it returned a
    # partial text fragment. It must never be reported as a clean completion.
    state = SessionState(messages=_convo())
    llm = FakeLLM([llm_response(content=content, finish_reason=finish_reason)])
    runner = build_runner(state=state, llm=llm)

    result = run(runner.run_loop())

    assert result == (content or "")
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "output truncated: provider reached its generation limit"
    assert len(llm.calls) == 1  # no retry attempted


def test_length_finish_reason_never_executes_or_persists_partial_tool_calls():
    state = SessionState(messages=_convo())
    tool_execution = FakeToolExecution()
    llm = FakeLLM(
        [
            llm_response(
                content="partial",
                tool_calls=[tool_call(arguments='{"path": "unterminated')],
                finish_reason="length",
            )
        ]
    )
    runner = build_runner(state=state, llm=llm, tool_execution=tool_execution)

    assert run(runner.run_loop()) == "partial"
    assert state.phase is SessionPhase.STOPPED
    assert tool_execution.calls == []
    assert not any(message.get("tool_calls") for message in state.messages)

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


def test_a_session_stopped_by_its_step_ceiling_says_so_on_disk():
    """The ceiling is a guard, so a run has to be able to prove it never fired.

    Tokens are the resource these arms are held to; steps vary and are counted.
    A session stopped at its step ceiling with budget still unspent is that
    story falling apart, and before this record it looked identical to a session
    that simply finished: one STOPPED phase, a reason string that lived only in
    memory, nothing in the trajectory.
    """
    tracer = FakeTracer()
    calls = [tool_call()]
    runner = build_runner(
        agent=FakeAgent([{"type": "function", "function": {"name": "fake_tool"}}]),
        # Never finishes on its own: every turn asks for another tool call, so
        # the only thing that can stop it is a ceiling.
        llm=FakeLLM(
            [
                llm_response(
                    content="still working",
                    tool_calls=calls,
                    total_tokens=5,
                    finish_reason="tool_calls",
                )
            ]
            * 6
        ),
        tool_execution=FakeToolExecution(
            ToolProcessingResult(
                messages_to_append=[
                    {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
                ]
            )
        ),
        tracer=tracer,
        max_steps=2,
        max_budget_tokens=1_000_000,
    )

    run(runner.run_loop())

    terminal = [step for step in tracer.steps if step["step_type"] == "session_terminal"]
    assert len(terminal) == 1
    payload = terminal[0]["payload"]
    assert payload["step_ceiling_reached"] is True
    assert payload["terminal_reason"] == "step limit reached: 2 steps"
    # The other resource was nowhere near spent, which is what makes the stop a
    # limit acting rather than a run finishing.
    assert payload["used_tokens"] < payload["max_budget_tokens"]


def test_a_session_that_finishes_on_its_own_reports_an_unreached_ceiling():
    """The control. Without it the field above proves nothing: a record that
    said "reached" for every session would pass the test above unchanged."""
    tracer = FakeTracer()
    runner = build_runner(
        llm=FakeLLM([llm_response(content="done", total_tokens=5)]),
        tracer=tracer,
        max_steps=50,
    )

    run(runner.run_loop())

    payload = [s for s in tracer.steps if s["step_type"] == "session_terminal"][0]["payload"]
    assert payload["step_ceiling_reached"] is False
    assert payload["step_count"] < payload["max_steps"]


def test_a_finished_session_re_entered_for_its_answer_does_not_sign_off_twice():
    """``run_loop`` doubles as a read-only query on a DONE session. One session
    is one disposition; a second row would double-count it."""
    tracer = FakeTracer()
    runner = build_runner(
        llm=FakeLLM([llm_response(content="done", total_tokens=5)]),
        tracer=tracer,
    )

    run(runner.run_loop())
    run(runner.run_loop())

    assert len([s for s in tracer.steps if s["step_type"] == "session_terminal"]) == 1



# --------------------------------------------------------------------------- #
# reasoning_content is not resent when streaming, so it must not be reserved
# --------------------------------------------------------------------------- #


def _reasoning_heavy_messages(reasoning_chars: int = 600_000):
    """A history whose bulk is recorded chain-of-thought, as the batches had.

    Streaming is the only way ``reasoning_content`` becomes non-empty, and the
    outbound normalizer strips it from every streaming request, so this history
    costs the provider far less input than a naive field-set estimate says.
    """
    return [
        {"role": "system", "content": "You are a careful engineer."},
        {"role": "user", "content": "Fix the failing test."},
        {
            "role": "assistant",
            "content": "Looking at the module now.",
            "reasoning_content": "step " * (reasoning_chars // 5),
        },
        {"role": "user", "content": "Continue."},
    ]


def _without_reasoning_content(messages):
    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


def test_reservation_without_reasoning_equals_reservation_of_stripped_history():
    """The streaming reservation must price the request the adapter will send.

    ``openai_provider._build_request_kwargs`` passes
    ``keep_reasoning_content=not stream``, so on a streaming call the request
    body is exactly this history minus ``reasoning_content``. Estimating that
    body is the whole fix: equality here, not a ratio, is the contract.
    """
    messages = _reasoning_heavy_messages()
    stripped = _without_reasoning_content(messages)

    kept = estimate_request_tokens(messages)
    dropped = estimate_request_tokens(messages, keep_reasoning_content=False)

    assert dropped == estimate_request_tokens(stripped)
    assert dropped == estimate_request_tokens(
        stripped, keep_reasoning_content=False
    )
    # Not a marginal correction: recorded reasoning was the bulk of the input.
    assert kept > 3 * dropped

    # Same contract for the per-message half the shaping layers call.
    assert estimate_request_message_tokens(
        messages, keep_reasoning_content=False
    ) == estimate_request_message_tokens(stripped)


def test_reservation_default_still_counts_reasoning_content():
    """Non-streaming callers resend recorded reasoning, so nothing changes.

    The keyword defaults to True precisely so every existing call site keeps
    its old number; a regression here is a silent behaviour change for the
    non-streaming baseline.
    """
    messages = _reasoning_heavy_messages(reasoning_chars=60_000)
    stripped = _without_reasoning_content(messages)

    assert estimate_request_tokens(messages) > estimate_request_tokens(stripped)
    assert estimate_request_message_tokens(
        messages
    ) > estimate_request_message_tokens(stripped)


def _streaming_agent(stream: bool):
    agent = FakeAgent()
    agent.llm_stream_chat = stream
    return agent


def test_streaming_session_is_not_stopped_by_reasoning_it_will_not_resend():
    """The astropy-14096 shape: budget above the real input, below the naive one.

    With streaming on the request carries no ``reasoning_content``, so a seat
    holding more than the stripped estimate has real headroom and the call must
    go out. Counting the reasoning here is what stopped 89 sessions before a
    single model call.
    """
    messages = _reasoning_heavy_messages()
    naive = estimate_request_tokens(messages)
    honest = estimate_request_tokens(messages, keep_reasoning_content=False)
    budget = honest + 20_000
    assert budget < naive

    llm = FakeLLM([llm_response(content="done", input_tokens=10, total_tokens=20)])
    state = SessionState(messages=list(messages))
    runner = build_runner(
        state=state,
        agent=_streaming_agent(True),
        llm=llm,
        max_budget_tokens=budget,
    )

    assert run(runner.run_loop()) == "done"
    assert len(llm.calls) == 1
    assert state.phase is not SessionPhase.STOPPED or not (
        state.terminal_reason or ""
    ).startswith("budget exhausted before model call")


def test_non_streaming_session_still_reserves_reasoning_it_will_resend():
    """Positive control for the flag: same history, streaming off, still stops.

    Without this the previous test would pass for the wrong reason (e.g. the
    reservation dropped out entirely rather than following the adapter).
    """
    messages = _reasoning_heavy_messages()
    honest = estimate_request_tokens(messages, keep_reasoning_content=False)
    budget = honest + 20_000

    llm = FakeLLM([llm_response(content="should not run")])
    state = SessionState(messages=list(messages))
    runner = build_runner(
        state=state,
        agent=_streaming_agent(False),
        llm=llm,
        max_budget_tokens=budget,
    )

    assert run(runner.run_loop()) == ""
    assert llm.calls == []
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason.startswith("budget exhausted before model call")
