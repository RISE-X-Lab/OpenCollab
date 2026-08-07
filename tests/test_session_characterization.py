"""Core session behavior characterization tests."""

from __future__ import annotations

import asyncio
import copy

import pytest
from session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    FakeTool,
    FakeTracer,
    event_collector,
    llm_response,
    run,
    tool_call,
)

from opencollab.application.event_bus import EventBus
from opencollab.application.session import SessionBusyError
from opencollab.application.tool_execution import CallbackPermissionPolicy
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import snapshot_session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.session import SessionPhase


def test_event_bus_accepts_sink_and_swallows_sink_exception():
    class BadSink:
        async def emit(self, _event):
            raise RuntimeError("sink failed")

    run(EventBus(BadSink()).emit(SessionEvent(type="error", data={"reason": "boom"})))

def test_event_bus_accepts_sync_and_async_callbacks():
    events = []

    def sync_callback(event):
        events.append(("sync", event.type))

    async def async_callback(event):
        events.append(("async", event.type))

    bus = EventBus(sync_callback)
    run(bus.emit(SessionEvent(type="sync_event")))
    bus2 = EventBus(async_callback)
    run(bus2.emit(SessionEvent(type="async_event")))

    assert events == [("sync", "sync_event"), ("async", "async_event")]

@pytest.mark.asyncio
async def test_event_bus_awaits_task_and_future_callbacks_in_order():
    events: list[str] = []

    async def task_work():
        events.append("task-start")
        await asyncio.sleep(0)
        events.append("task-end")

    def task_callback(event):
        return asyncio.create_task(task_work())

    def future_callback(event):
        events.append("future-start")
        future = asyncio.get_running_loop().create_future()

        def finish():
            events.append("future-end")
            future.set_result(None)

        asyncio.get_running_loop().call_soon(finish)
        return future

    def final_callback(event):
        events.append("final")

    bus = EventBus(task_callback)
    bus.subscribe(future_callback)
    bus.subscribe(final_callback)
    await bus.emit(SessionEvent(type="ordered"))

    assert events == [
        "task-start",
        "task-end",
        "future-start",
        "future-end",
        "final",
    ]

def test_event_bus_accepts_sync_sink():
    events = []

    class SyncSink:
        def emit(self, event):
            events.append(event.type)

    run(EventBus(SyncSink()).emit(SessionEvent(type="sink_event")))

    assert events == ["sink_event"]

def test_session_auto_save_path_is_public():
    fake_llm = FakeLLMClient()
    with_path = Session(agent=FakeAgent(), llm=fake_llm, auto_save_path="foo.jsonl")
    without_path = Session(agent=FakeAgent(), llm=fake_llm)

    assert with_path.auto_save_path == "foo.jsonl"
    assert without_path.auto_save_path is None

def test_add_user_message_appends_resets_hashes_and_autosaves():
    class FakeStore:
        def __init__(self):
            self.save_calls = []

        def save(self, path, messages, *, meta=None):
            self.save_calls.append((path, copy.deepcopy(messages), meta))

    store = FakeStore()
    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        store=store,
        auto_save_path="autosave.jsonl",
    )
    session.is_done = True
    session._recent_call_hashes.extend(["hash-1", "hash-2"])

    run(session.add_user_message("hello"))

    assert session.messages[-1] == {"role": "user", "content": "hello"}
    assert session.is_done is False
    assert session._recent_call_hashes == []
    assert len(store.save_calls) == 1
    saved_path, saved_messages, _ = store.save_calls[0]
    assert saved_path == "autosave.jsonl"
    assert saved_messages[-1]["role"] == "user"
    assert saved_messages[-1]["content"] == "hello"
    assert "timestamp" in saved_messages[-1]


def test_session_rejects_a_second_queued_user_turn_before_the_first_runs():
    session = Session(agent=FakeAgent(), llm=FakeLLMClient())
    run(session.add_user_message("first queued turn"))
    before = copy.deepcopy(session.messages)

    with pytest.raises(SessionBusyError, match="active turn"):
        run(session.add_user_message("must not merge into the first turn"))

    assert session.messages == before
    assert session.state.pending_external_user_turn is not None
    assert session.state.pending_external_user_turn["content"] == "first queued turn"


@pytest.mark.asyncio
async def test_session_rejects_a_second_runner_while_a_turn_is_active():
    class GatedLLM:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools=None, temperature=0.0):
            del messages, tools, temperature
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return llm_response(content="done")

    llm = GatedLLM()
    session = Session(agent=FakeAgent(), llm=llm)
    first = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(llm.started.wait(), timeout=0.5)
    second = asyncio.create_task(session.run_loop())

    try:
        for _ in range(5):
            if second.done():
                break
            await asyncio.sleep(0)
        assert second.done()
        assert isinstance(second.exception(), SessionBusyError)
        assert llm.calls == 1
    finally:
        llm.release.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_rejects_user_message_while_provider_turn_is_active():
    class GatedLLM:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools=None, temperature=0.0):
            del messages, tools, temperature
            self.started.set()
            await self.release.wait()
            return llm_response(content="done")

    llm = GatedLLM()
    session = Session(agent=FakeAgent(), llm=llm)
    run_task = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(llm.started.wait(), timeout=0.5)
    before = list(session.messages)

    try:
        with pytest.raises(SessionBusyError, match="active turn"):
            await session.add_user_message("must not interleave")
        assert session.messages == before
    finally:
        llm.release.set()
        await run_task


def test_session_rejects_user_message_for_a_suspended_turn():
    session = Session(agent=FakeAgent(), llm=FakeLLMClient())
    session.phase = SessionPhase.AWAITING_EVENTS
    before = list(session.messages)

    with pytest.raises(SessionBusyError, match="active turn"):
        run(session.add_user_message("must not replace suspended turn"))

    assert session.messages == before


def test_snapshot_preserves_historical_subset_only():
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.messages.append({"role": "assistant", "content": "old answer"})
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    session.phase = SessionPhase.DONE
    session._recent_call_hashes.append("hash-1")

    snap = snapshot_session(session)

    assert snap is not session
    assert snap.agent is agent
    assert snap.messages == session.messages
    assert snap.messages is not session.messages
    assert snap.messages[0] is not session.messages[0]
    assert snap.used_tokens == 123
    assert snap.step_count == 4
    assert snap.is_done is False
    assert snap.phase == SessionPhase.IDLE
    assert snap._recent_call_hashes == []

def test_budget_exceeded_stops_before_llm_call_and_emits_error():
    fake_llm = FakeLLMClient()
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(),
        llm=fake_llm,
        max_budget_tokens=10,
        event_sink=EventBus(on_event),
    )
    session.used_tokens = 10

    result = run(session.run_loop())

    assert result == ""
    assert fake_llm.calls == []
    assert session.is_done is False
    assert session.messages[-1] == {
        "role": "system",
        "content": "[Budget exceeded: 10 tokens used. Session stopped.]",
    }
    assert [(event.type, event.data) for event in events] == [
        ("error", {"reason": "budget exceeded: 10 tokens used", "aid": -1})
    ]

def test_run_loop_does_not_route_to_mutating_compaction():
    # The mutating compaction path is gone: read-time shaping (AutoCompactShaper)
    # owns compaction instead, so even a long history costs no extra
    # summarization turn — the first model response is the answer.
    fake_llm = FakeLLMClient([
        llm_response(content="direct answer", input_tokens=3, output_tokens=4),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(),
        llm=fake_llm,
        tracer=tracer,
        event_sink=EventBus(on_event),
    )
    session.messages.extend({"role": "user", "content": f"message {idx}"} for idx in range(10))

    result = run(session.run_loop())

    assert result == "direct answer"
    assert len(fake_llm.calls) == 1
    assert "compaction" not in [event.type for event in events]
    assert all(step["step_type"] != "compaction" for step in tracer.steps)

def test_no_tool_calls_marks_done_and_emits_text_delta():
    fake_llm = FakeLLMClient([
        llm_response(content="plain answer", input_tokens=2, output_tokens=3),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(),
        llm=fake_llm,
        tracer=tracer,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "plain answer"
    assert session.is_done is True
    assert session.step_count == 1
    assert session.used_tokens == 5
    assert session.messages[-1] == {"role": "assistant", "content": "plain answer"}
    assert [event.type for event in events] == ["step_start", "text_delta", "step_end"]
    assert fake_llm.calls[0]["tools"] is None
    assert tracer.steps[0]["step_type"] == "llm_call"
    assert tracer.steps[0]["payload"]["content"] == "plain answer"

def test_session_accepts_explicit_llm_client():
    fake_llm = FakeLLMClient([
        llm_response(content="explicit llm answer", input_tokens=2, output_tokens=3),
    ])
    session = Session(agent=FakeAgent(), llm=fake_llm)

    assert session._llm is fake_llm
    assert session.runner.llm is fake_llm

    result = run(session.run_loop())

    assert result == "explicit llm answer"
    assert fake_llm.calls

def test_session_event_sink_wires_through_to_runtime():
    fake_llm = FakeLLMClient([
        llm_response(content="event sink answer"),
    ])
    sink_events = []

    class Sink:
        async def emit(self, event):
            sink_events.append(event.type)

    sink = Sink()
    session = Session(agent=FakeAgent(), llm=fake_llm, event_sink=sink)

    assert session.event_bus.sink is sink
    assert session.runner.event_publisher is session.event_bus
    assert session.tool_execution.event_publisher is session.event_bus

    result = run(session.run_loop())

    assert result == "event sink answer"
    assert sink_events == ["step_start", "text_delta", "step_end"]

def test_run_loop_when_already_done_returns_latest_assistant_without_llm_call():
    fake_llm = FakeLLMClient()
    session = Session(agent=FakeAgent(), llm=fake_llm)
    session.messages.append({"role": "assistant", "content": "already done"})
    session.is_done = True

    result = run(session.run_loop())

    assert result == "already done"
    assert fake_llm.calls == []

def test_run_loop_with_zero_max_steps_exits_without_llm_call():
    fake_llm = FakeLLMClient()
    session = Session(agent=FakeAgent(), llm=fake_llm, max_steps=0)

    result = run(session.run_loop())

    assert result == ""
    assert fake_llm.calls == []
    assert session.step_count == 0
    assert session.is_done is False
    assert session.phase == SessionPhase.STOPPED
    assert session.state.terminal_reason == "step limit reached: 0 steps"

def test_session_runner_facade_hides_private_response_handler():
    session = Session(agent=FakeAgent(), llm=FakeLLMClient())

    assert not hasattr(session.runner, "_handle_pending_response")

def test_tool_calls_execute_append_tool_result_and_continue():
    tool = FakeTool(result=lambda args: f"echo {args['value']}")
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 7}')], finish_reason="tool_calls"),
        llm_response(content="done"),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()

    async def confirm_fn(_prompt):
        return True

    session = Session(
        agent=FakeAgent(tools=[tool]),
        llm=fake_llm,
        tracer=tracer,
        event_sink=EventBus(on_event),
        permission_policy=CallbackPermissionPolicy(confirm_fn),
    )

    result = run(session.run_loop())

    assert result == "done"
    assert session.step_count == 2
    assert tool.calls[0]["args"] == {"value": 7}
    assert tool.calls[0]["env"] is session.env
    assert tool.calls[0]["confirm_fn"] is not None
    assert run(tool.calls[0]["confirm_fn"]("confirm?")) is True
    assert session.messages[1]["role"] == "assistant"
    assert session.messages[1]["tool_calls"][0]["function"]["name"] == "fake_tool"
    assert session.messages[2] == {"role": "tool", "tool_call_id": "call-1", "content": "echo 7"}
    assert session.messages[3] == {"role": "assistant", "content": "done"}
    assert [event.type for event in events] == [
        "step_start",
        "tool_start",
        "tool_end",
        "step_end",
        "step_start",
        "text_delta",
        "step_end",
    ]
    assert [step["step_type"] for step in tracer.steps] == ["llm_call", "tool_exec", "llm_call"]
    assert fake_llm.calls[0]["tools"][0]["function"]["name"] == "fake_tool"

@pytest.mark.parametrize(
    ("error", "final_answer", "expected_content"),
    [
        (PermissionError("blocked"), "after permission", "Permission denied: blocked"),
        (ValueError("bad value"), "after exception", "Tool execution error: ValueError: bad value"),
    ],
    ids=["permission-error", "tool-exception"],
)
def test_tool_errors_are_returned_as_tool_messages(error, final_answer, expected_content):
    tool = FakeTool(exc=error)
    fake_llm = FakeLLMClient(
        [
            llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
            llm_response(content=final_answer),
        ]
    )
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == final_answer
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": expected_content,
    }
    assert [event.type for event in events[:3]] == ["step_start", "tool_start", "tool_end"]

@pytest.mark.parametrize(
    ("call", "expected_content"),
    [
        (tool_call(arguments="{not json"), "Error: invalid JSON arguments: {not json"),
        (
            tool_call(name="missing_tool", arguments='{"value": 1}'),
            "Error: unknown tool 'missing_tool'. Available: ['known_tool']",
        ),
    ],
    ids=["invalid-json", "unknown-tool"],
)
def test_invalid_tool_calls_append_error_without_execution(call, expected_content):
    known_tool = FakeTool(name="known_tool")
    fake_llm = FakeLLMClient(
        [
            llm_response(tool_calls=[call], finish_reason="tool_calls"),
            llm_response(content="recovered"),
        ]
    )
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[known_tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "recovered"
    assert known_tool.calls == []
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": expected_content,
    }
    assert "tool_start" not in [event.type for event in events]

def test_loop_detection_skips_third_identical_tool_call():
    tool = FakeTool(result="same result")
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(call_id="call-1", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(tool_calls=[tool_call(call_id="call-2", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(tool_calls=[tool_call(call_id="call-3", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="escaped loop"),
    ])
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "escaped loop"
    assert len(tool.calls) == 2
    loop_messages = [msg for msg in session.messages if msg.get("content", "").startswith("[Loop detected:")]
    assert loop_messages == [{
        "role": "tool",
        "tool_call_id": "call-3",
        "content": (
            "[Loop detected: tool 'fake_tool' called 3 times with identical arguments. "
            "You are stuck in a loop. Try a completely different approach or ask for help.]"
        ),
    }]
    assert [(event.type, event.data) for event in events if event.type == "loop_detected"] == [
        ("loop_detected", {"tool": "fake_tool", "count": 3, "aid": -1})
    ]

def test_tool_output_is_persisted_in_full_in_messages():
    # The full tool result is now persisted verbatim in the message history;
    # the per-tool-result budget shaper caps only the model-facing copy at call
    # time (see test_shaping / test_session_run_loop).
    long_result = "a" * 25_000 + "b" * 123 + "c" * 25_000
    tool = FakeTool(result=long_result)
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="done"),
    ])
    session = Session(agent=FakeAgent(tools=[tool]), llm=fake_llm)

    result = run(session.run_loop())

    assert result == "done"
    assert session.messages[2]["content"] == long_result

def test_event_callback_exception_is_swallowed():
    fake_llm = FakeLLMClient([
        llm_response(content="answer despite bad callback"),
    ])

    def bad_on_event(_event):
        raise RuntimeError("callback failed")

    session = Session(agent=FakeAgent(), llm=fake_llm, event_sink=EventBus(bad_on_event))

    result = run(session.run_loop())

    assert result == "answer despite bad callback"
    assert session.is_done is True
    assert session.messages[-1] == {"role": "assistant", "content": "answer despite bad callback"}
