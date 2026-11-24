"""Core session behavior characterization tests."""

from __future__ import annotations

import asyncio
import copy

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.application.session import SessionBusyError
from opencollab.application.tool_execution import CallbackPermissionPolicy
from opencollab.bootstrap import SnapshotSessionError, snapshot_session
from opencollab.bootstrap import build_session as Session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.session_characterization_test_support import (
    FakeAgent,
    FakeLLMClient,
    FakeTool,
    FakeTracer,
    event_collector,
    llm_response,
    run,
    tool_call,
)
from tests.support.session_run_loop_test_support import FakeLLM


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

def test_session_auto_save_path_is_public(local_environment_factory):
    fake_llm = FakeLLMClient()
    with_path = Session(env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm, auto_save_path="foo.jsonl")
    without_path = Session(env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm)

    assert with_path.auto_save_path == "foo.jsonl"
    assert without_path.auto_save_path is None

def test_add_user_message_appends_resets_hashes_and_autosaves(local_environment_factory):
    class FakeStore:
        def __init__(self):
            self.save_calls = []

        def save(self, path, messages, *, meta=None):
            self.save_calls.append((path, copy.deepcopy(messages), meta))

    store = FakeStore()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(),
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

def test_new_user_turn_restores_agent_tools_and_wind_down_latches(local_environment_factory):
    original_tool = object()
    forced_submit_tool = object()
    agent = FakeAgent(tools=[original_tool])
    agent.tool_choice = None
    session = Session(env=local_environment_factory(), agent=agent, llm=FakeLLMClient())
    session.agent.tools = [forced_submit_tool]
    session.agent.tool_choice = {
        "type": "function",
        "function": {"name": "submit_findings"},
    }
    session.state.wind_down_done = True
    session.state.wind_down_attempts = 2
    session.state.wind_down_token_mark = 123
    session.state.set_phase(SessionPhase.STOPPED)

    run(session.add_user_message("start a fresh turn"))

    assert session.agent.tools == [original_tool]
    assert session.agent.tool_choice is None
    assert session.state.wind_down_done is False
    assert session.state.wind_down_attempts == 0
    assert session.state.wind_down_token_mark == 0

def test_session_rejects_a_second_queued_user_turn_before_the_first_runs(local_environment_factory):
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=FakeLLMClient())
    run(session.add_user_message("first queued turn"))
    before = copy.deepcopy(session.messages)

    with pytest.raises(SessionBusyError, match="active turn"):
        run(session.add_user_message("must not merge into the first turn"))

    assert session.messages == before
    assert session.state.pending_external_user_turn is not None
    assert session.state.pending_external_user_turn["content"] == "first queued turn"

@pytest.mark.asyncio
async def test_session_rejects_a_second_runner_while_a_turn_is_active(local_environment_factory):
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
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=llm)
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
async def test_session_rejects_user_message_while_provider_turn_is_active(local_environment_factory):
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
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=llm)
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

@pytest.mark.asyncio
async def test_direct_run_loop_cancellation_leaves_session_reusable(local_environment_factory):
    class GatedLLM:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()

        async def complete(self, messages, tools=None, temperature=0.0):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await asyncio.Event().wait()
            return llm_response(content="fresh answer")

    llm = GatedLLM()
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=llm)
    cancelled = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(llm.started.wait(), timeout=0.5)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled, timeout=0.5)

    assert session.state.phase is SessionPhase.STOPPED
    assert session.state.terminal_reason == "cancelled"

    await session.add_user_message("retry after cancellation")
    assert await asyncio.wait_for(session.run_loop(), timeout=0.5) == "fresh answer"
    assert session.state.phase is SessionPhase.DONE
    assert llm.calls == 2

def test_session_rejects_user_message_for_a_suspended_turn(local_environment_factory):
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=FakeLLMClient())
    session.phase = SessionPhase.AWAITING_EVENTS
    before = list(session.messages)

    with pytest.raises(SessionBusyError, match="active turn"):
        run(session.add_user_message("must not replace suspended turn"))

    assert session.messages == before

def test_snapshot_rejects_an_environment_without_a_safe_fork(local_environment_factory):
    agent = FakeAgent()
    session = Session(env=local_environment_factory(), agent=agent, llm=FakeLLMClient())
    session.messages.append({"role": "assistant", "content": "old answer"})
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    session.phase = SessionPhase.DONE
    session._recent_call_hashes.append("hash-1")

    with pytest.raises(SnapshotSessionError, match="environment.fork_snapshot"):
        snapshot_session(session)

def test_snapshot_copies_full_state_and_isolates_agent_and_environment():
    class ForkableEnvironment:
        workspace = "."
        host_workspace = "."
        source_workspace = "."
        local_filesystem = False

        def fork_snapshot(self):
            return ForkableEnvironment()

    agent = FakeAgent()
    agent.tools = [FakeTool("snapshot-tool")]
    session = Session(agent=agent, env=ForkableEnvironment(), llm=FakeLLMClient())
    state = session.state
    state.context_tokens = 17
    state.markup_recovered = 3
    state.wind_down_done = True
    state.wind_down_attempts = 2
    state.wind_down_token_mark = 99
    state.phase = SessionPhase.ERROR
    state.terminal_reason = "provider failed"
    state.aid = 7
    state.pending_events.add(
        PendingRow(
            tool_call_id="pending-1",
            kind=RowKind.CHILD_AGENT,
            order=0,
            status=RowStatus.PENDING,
        )
    )

    snap = snapshot_session(session)

    assert snap.agent is not session.agent
    assert snap.env is not session.env
    assert snap.state is not state
    assert snap.state.context_tokens == 17
    assert snap.state.markup_recovered == 3
    assert snap.state.wind_down_attempts == 2
    assert snap.state.phase is SessionPhase.ERROR
    assert snap.state.terminal_reason == "provider failed"
    assert snap.state.aid == 7
    assert "pending-1" in snap.state.pending_events.rows

    snap.agent.tools.clear()
    snap.state.pending_events.clear()
    assert session.agent.tools
    assert "pending-1" in state.pending_events.rows

def test_budget_exceeded_stops_before_llm_call_and_emits_error(local_environment_factory):
    fake_llm = FakeLLMClient()
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(),
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

def test_run_loop_does_not_route_to_mutating_compaction(local_environment_factory):
    # Read-time shaping compacts history without an extra summarization turn.
    fake_llm = FakeLLMClient([
        llm_response(content="direct answer", input_tokens=3, output_tokens=4),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(),
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

def test_no_tool_calls_marks_done_and_emits_text_delta(local_environment_factory):
    fake_llm = FakeLLMClient([
        llm_response(content="plain answer", input_tokens=2, output_tokens=3),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(),
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
    assert [step["step_type"] for step in tracer.steps] == [
        "session.history_compaction",
        "context_shaping",
        "llm_call_started",
        "llm_call",
        "session_terminal",
    ]
    assert tracer.steps[3]["payload"]["content"] == "plain answer"

@pytest.mark.parametrize(
    ("output_tokens", "phase", "reason"),
    [
        (7, SessionPhase.DONE, "completed"),
        (4_000, SessionPhase.STOPPED, "budget exceeded after model call: 4006 tokens used"),
    ],
)
async def test_new_user_turn_records_its_terminal_phase(local_environment_factory, output_tokens, phase, reason):
    llm = FakeLLM([
        llm_response(content="first answer", input_tokens=2, output_tokens=3),
        llm_response(content="second answer", output_tokens=output_tokens),
    ])
    tracer = FakeTracer()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(), llm=llm,
        tracer=tracer, max_budget_tokens=2_000,
    )

    await session.add_user_message("First question")
    assert await session.run_loop() == "first answer"
    assert await session.run_loop() == "first answer"
    await session.add_user_message("Second question")
    assert await session.run_loop() == "second answer"

    assert session.phase is phase
    assert len(llm.calls) == 2
    terminal = [step["payload"] for step in tracer.steps if step["step_type"] == "session_terminal"]
    assert [(row["phase"], row["terminal_reason"], row["used_tokens"], row["step_count"]) for row in terminal] == [
        ("done", "completed", 5, 1),
        (phase.value, reason, 6 + output_tokens, 2),
    ]

async def test_filtered_reply_rereads_share_one_terminal_trace_per_turn(local_environment_factory):
    llm = FakeLLMClient([
        llm_response(content="first partial answer", finish_reason="content_filter"),
        llm_response(content="second partial answer", finish_reason="content_filter"),
    ])
    tracer = FakeTracer()
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=llm, tracer=tracer)

    await session.add_user_message("First question")
    assert await session.run_loop() == "first partial answer"
    assert await session.run_loop() == "first partial answer"
    assert len(llm.calls) == 1

    await session.add_user_message("Second question")
    assert await session.run_loop() == "second partial answer"
    assert await session.run_loop() == "second partial answer"
    assert len(llm.calls) == 2

    terminal = [step["payload"] for step in tracer.steps if step["step_type"] == "session_terminal"]
    assert [(row["phase"], row["terminal_reason"], row["step_count"]) for row in terminal] == [
        ("stopped", "provider filtered the output", 1),
        ("stopped", "provider filtered the output", 2),
    ]

def test_session_event_sink_wires_through_to_runtime(local_environment_factory):
    fake_llm = FakeLLMClient([
        llm_response(content="event sink answer"),
    ])
    sink_events = []

    class Sink:
        async def emit(self, event):
            sink_events.append(event.type)

    sink = Sink()
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm, event_sink=sink)

    assert session.event_bus.sink is sink
    assert session.runner.event_publisher is session.event_bus
    assert session.tool_execution.event_publisher is session.event_bus

    result = run(session.run_loop())

    assert result == "event sink answer"
    assert sink_events == ["step_start", "text_delta", "step_end"]

def test_run_loop_when_already_done_returns_latest_assistant_without_llm_call(local_environment_factory):
    fake_llm = FakeLLMClient()
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm)
    session.messages.append({"role": "assistant", "content": "already done"})
    session.is_done = True

    result = run(session.run_loop())

    assert result == "already done"
    assert fake_llm.calls == []

def test_run_loop_with_zero_max_steps_exits_without_llm_call(local_environment_factory):
    fake_llm = FakeLLMClient()
    session = Session(env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm, max_steps=0)

    result = run(session.run_loop())

    assert result == ""
    assert fake_llm.calls == []
    assert session.step_count == 0
    assert session.is_done is False
    assert session.phase == SessionPhase.STOPPED
    assert session.state.terminal_reason == "step limit reached: 0 steps"

def test_tool_calls_execute_append_tool_result_and_continue(local_environment_factory):
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
        env=local_environment_factory(), agent=FakeAgent(tools=[tool]),
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
    assert [step["step_type"] for step in tracer.steps] == [
        "session.history_compaction",
        "context_shaping",
        "llm_call_started",
        "llm_call",
        "tool_exec",
        "context_shaping",
        "llm_call_started",
        "llm_call",
        "session_terminal",
    ]
    assert fake_llm.calls[0]["tools"][0]["function"]["name"] == "fake_tool"

@pytest.mark.parametrize(
    ("error", "final_answer", "expected_content"),
    [
        (PermissionError("blocked"), "after permission", "Permission denied: blocked"),
        (ValueError("bad value"), "after exception", "Tool execution error: ValueError: bad value"),
    ],
    ids=["permission-error", "tool-exception"],
)
def test_tool_errors_are_returned_as_tool_messages(local_environment_factory, error, final_answer, expected_content):
    tool = FakeTool(exc=error)
    fake_llm = FakeLLMClient(
        [
            llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
            llm_response(content=final_answer),
        ]
    )
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(tools=[tool]),
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
def test_invalid_tool_calls_append_error_without_execution(local_environment_factory, call, expected_content):
    known_tool = FakeTool(name="known_tool")
    fake_llm = FakeLLMClient(
        [
            llm_response(tool_calls=[call], finish_reason="tool_calls"),
            llm_response(content="recovered"),
        ]
    )
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(tools=[known_tool]),
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

def test_loop_detection_skips_third_identical_tool_call(local_environment_factory):
    tool = FakeTool(result="same result")
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(call_id="call-1", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(tool_calls=[tool_call(call_id="call-2", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(tool_calls=[tool_call(call_id="call-3", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="escaped loop"),
    ])
    events, on_event = event_collector()
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(tools=[tool]),
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

def test_tool_output_is_persisted_in_full_in_messages(local_environment_factory):
    # The tool-result shaper caps the model-facing copy while history retains the full result.
    long_result = "a" * 25_000 + "b" * 123 + "c" * 25_000
    tool = FakeTool(result=long_result)
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="done"),
    ])
    session = Session(env=local_environment_factory(), agent=FakeAgent(tools=[tool]), llm=fake_llm)

    result = run(session.run_loop())

    assert result == "done"
    assert session.messages[2]["content"] == long_result

def test_event_callback_exception_is_swallowed(local_environment_factory):
    fake_llm = FakeLLMClient([
        llm_response(content="answer despite bad callback"),
    ])

    def bad_on_event(_event):
        raise RuntimeError("callback failed")

    session = Session(
        env=local_environment_factory(), agent=FakeAgent(), llm=fake_llm, event_sink=EventBus(bad_on_event))

    result = run(session.run_loop())

    assert result == "answer despite bad callback"
    assert session.is_done is True
    assert session.messages[-1] == {"role": "assistant", "content": "answer despite bad callback"}
