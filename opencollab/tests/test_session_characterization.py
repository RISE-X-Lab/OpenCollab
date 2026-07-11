import asyncio
import copy

import pytest
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.application.event_bus import EventBus
from opencollab.application.tool_execution import CallbackPermissionPolicy
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session, snapshot_session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)


def llm_response(content=None, tool_calls=None, input_tokens=1, output_tokens=1, finish_reason="stop"):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason=finish_reason,
    )


def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeLLMClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append({
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "temperature": temperature,
        })
        if not self.responses:
            raise AssertionError("FakeLLMClient received an unexpected complete() call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAgent:
    def __init__(self, tools=None):
        self.name = "fake-agent"
        self.system_prompt = "You are a fake agent."
        self.tools = tools or []
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.api_key = "fake-key"
        self.base_url = "https://fake.invalid"
        self.temperature = 0.25

    def tool_schemas(self):
        return [tool.to_openai_schema() for tool in self.tools]

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name.lower() == name.lower():
                return tool
        return None


def fake_team_session_factory(*, safety_policy_factory=None, lead_workspace=None, interactive=False):
    from opencollab.bootstrap.container import DefaultSessionFactory, SpawnConfig

    event_bus = EventBus()
    factory = DefaultSessionFactory(
        SpawnConfig(
            model="fake-model",
            provider="fake-provider",
            api_key="fake-key",
            base_url=None,
            llm_timeout=600.0,
            tracer=None,
            event_bus=event_bus,
            permission_policy=None,
            safety_policy_factory=safety_policy_factory,
        ),
        lead_workspace=lead_workspace,
        interactive=interactive,
    )
    return event_bus, factory


def fake_worktree_pool():
    from opencollab.adapters.worktree_pool import WorktreePool

    return WorktreePool(".", use_worktrees=False)


class FakeTool:
    def __init__(self, name="fake_tool", result="tool result", exc=None):
        self.name = name
        self.result = result
        self.exc = exc
        self.calls = []

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fake tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute_with_runtime(self, args, runtime):
        self.calls.append({
            "args": copy.deepcopy(args),
            "env": runtime.environment,
            "interceptor": runtime.safety_policy,
            "confirm_fn": runtime.confirm_fn(),
        })
        if self.exc:
            raise self.exc
        return self.result(args) if callable(self.result) else self.result


class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens, latency):
        self.steps.append({
            "step_type": step_type,
            "payload": copy.deepcopy(payload),
            "tokens": tokens,
            "latency": latency,
        })

    def flush(self):
        self.flush_count += 1


def event_collector():
    events = []

    def on_event(event):
        events.append(event)

    return events, on_event


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


def test_run_loop_cancellation_appends_interruption_and_emits_error():
    fake_llm = FakeLLMClient()
    events, on_event = event_collector()
    session = Session(agent=FakeAgent(), llm=fake_llm, event_sink=EventBus(on_event))
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = run(session.run_loop(cancel_event=cancel_event))

    assert result == ""
    assert fake_llm.calls == []
    assert session.messages[-1] == {"role": "system", "content": "[Session interrupted by user]"}
    assert session.is_done is False
    assert [(event.type, event.data) for event in events] == [("error", {"reason": "cancelled", "aid": -1})]


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
    assert [(event.type, event.data) for event in events] == [("error", {"reason": "budget_exceeded", "aid": -1})]


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
    assert session.phase == SessionPhase.STEP_LIMIT_EXCEEDED
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


def test_tool_processor_returns_result_before_state_application():
    tool = FakeTool(result=lambda args: f"echo {args['value']}")
    session = Session(agent=FakeAgent(tools=[tool]), llm=FakeLLMClient())
    original_messages = copy.deepcopy(session.messages)

    result = run(session.tool_execution.process([tool_call(arguments='{"value": 9}')]))

    assert isinstance(result, ToolProcessingResult)
    assert result.messages_to_append == [{"role": "tool", "tool_call_id": "call-1", "content": "echo 9"}]
    assert len(result.recent_hash_updates) == 1
    assert session.messages == original_messages
    assert session._recent_call_hashes == []

    result.apply_to(session.state)

    assert session.messages[-1] == {"role": "tool", "tool_call_id": "call-1", "content": "echo 9"}
    assert session._recent_call_hashes == result.recent_hash_updates


def test_tool_permission_error_is_returned_as_tool_message():
    tool = FakeTool(exc=PermissionError("blocked"))
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="after permission"),
    ])
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "after permission"
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Permission denied: blocked",
    }
    assert [event.type for event in events[:3]] == ["step_start", "tool_start", "tool_end"]


def test_tool_exception_is_returned_as_tool_message():
    tool = FakeTool(exc=ValueError("bad value"))
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="after exception"),
    ])
    session = Session(agent=FakeAgent(tools=[tool]), llm=fake_llm)

    result = run(session.run_loop())

    assert result == "after exception"
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Tool execution error: ValueError: bad value",
    }


def test_invalid_json_tool_arguments_append_error_tool_message_without_execution():
    tool = FakeTool()
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments="{not json")], finish_reason="tool_calls"),
        llm_response(content="recovered"),
    ])
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "recovered"
    assert tool.calls == []
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Error: invalid JSON arguments: {not json",
    }
    assert "tool_start" not in [event.type for event in events]


def test_unknown_tool_appends_available_tools_error():
    known_tool = FakeTool(name="known_tool")
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(name="missing_tool", arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="after unknown"),
    ])
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(tools=[known_tool]),
        llm=fake_llm,
        event_sink=EventBus(on_event),
    )

    result = run(session.run_loop())

    assert result == "after unknown"
    assert known_tool.calls == []
    assert session.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Error: unknown tool 'missing_tool'. Available: ['known_tool']",
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


# Characterizes historical/current behavior: mutating runtime config after
# Session construction desyncs facade fields from already-built runtime objects.
# This is not recommended; new code should inject env/max_steps via constructors.
def test_session_runtime_config_desync_after_mutating_env_and_max_steps():
    old_env = object()
    new_env = object()
    old_max_steps = 7
    new_max_steps = 3
    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        env=old_env,
        max_steps=old_max_steps,
    )

    session.env = new_env
    session.max_steps = new_max_steps

    assert session.env is new_env
    assert session.tool_execution.environment is old_env
    assert session.runner.max_steps == old_max_steps


def test_scheduler_init_process_lead_uses_workspace_local_env(tmp_path, monkeypatch):
    import os

    from opencollab.adapters.env import LocalEnvironment
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(lead_workspace=str(tmp_path))

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    lead = scheduler._lead_session
    assert isinstance(lead.env, LocalEnvironment)
    assert lead.env.workspace == os.path.abspath(str(tmp_path))
    assert lead.tool_execution.environment is lead.env
    tool_names = {t.name for t in lead.agent.tools}
    assert {"spawn_agent", "spawn_with_review"} <= tool_names


def test_scheduler_init_process_lead_gets_workspace_safety_policy(tmp_path, monkeypatch):
    from opencollab.adapters.safety import SandboxInterceptor
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(lead_workspace=str(tmp_path))

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    policy = scheduler._lead_session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())


def test_lead_safety_policy_is_independent_of_child_safety_factory(tmp_path, monkeypatch):
    from opencollab.adapters.safety import SandboxInterceptor
    from opencollab.application.scheduler import LaunchSpec, Scheduler
    from opencollab.bootstrap import container as session_module

    # The child-session ``safety_policy_factory`` does not govern the lead: the
    # lead always gets a workspace-rooted sandbox built from its local env.
    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(
        safety_policy_factory=None,
        lead_workspace=str(tmp_path),
    )

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=fake_worktree_pool(),
        event_sink=event_bus,
    )
    scheduler.create_init_process(LaunchSpec())

    policy = scheduler._lead_session.tool_execution.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())


def test_save_and_load_round_trip_restores_runtime_state(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    session.state.pending_user_messages = [
        {"role": "user", "content": "queued", "timestamp": "2026-01-01T00:00:00Z"}
    ]
    path = tmp_path / "session.jsonl"

    session.save(str(path))
    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.messages == session.messages
    assert loaded.used_tokens == 123
    assert loaded.step_count == 4
    assert loaded.is_done is True
    assert loaded.state.pending_user_messages == session.state.pending_user_messages


def test_save_and_load_round_trip_restores_control_flow_latches(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    state = session.state
    state.reads_since_last_edit = 7
    state.low_yield_since_progress = 3
    state.distinct_evidence_count = 4
    state._seen_result_hashes = {"content-hash", "call-hash"}
    state.scout_ledger = [{"tool": "grep", "outcome": "hit"}]
    state.steps_since_progress = 2
    state.wind_down_done = True
    state.wind_down_token_mark = 123
    state.forced_unsatisfied = True
    state.loop_blocked_since_progress = 2
    state.extension_offered = True
    state.extensions_granted = 1
    state.extension_reasons = ["need one exact signature"]
    path = tmp_path / "control-state.json"

    session.save(str(path))
    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    restored = loaded.state
    assert restored.reads_since_last_edit == 7
    assert restored.low_yield_since_progress == 3
    assert restored.distinct_evidence_count == 4
    assert restored._seen_result_hashes == {"content-hash", "call-hash"}
    assert restored.scout_ledger == [{"tool": "grep", "outcome": "hit"}]
    assert restored.steps_since_progress == 2
    assert restored.wind_down_done is True
    assert restored.wind_down_token_mark == 123
    assert restored.forced_unsatisfied is True
    assert restored.loop_blocked_since_progress == 2
    assert restored.extension_offered is True
    assert restored.extensions_granted == 1
    assert restored.extension_reasons == ["need one exact signature"]


def test_restore_pairs_reused_tool_call_ids_in_transcript_order(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())

    def reused_call():
        return {
            "id": "reused-id",
            "type": "function",
            "function": {"name": "grep", "arguments": "{}"},
        }

    session.state.append_message(
        {"role": "assistant", "content": None, "tool_calls": [reused_call()]}
    )
    session.state.append_message(
        {"role": "tool", "tool_call_id": "reused-id", "content": "old result"}
    )
    session.state.append_message(
        {"role": "assistant", "content": None, "tool_calls": [reused_call()]}
    )
    session.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "reused-tool-id.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    matching_results = [
        message
        for message in loaded.messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "reused-id"
    ]
    assert len(matching_results) == 2
    assert matching_results[-1]["content"] == "Tool execution interrupted by session restore."
    assert loaded.messages[-1] == matching_results[-1]
    assert loaded._open_tool_call_ids() == []


def test_restore_converts_orphaned_deferred_child_to_failed_tool_result(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.phase = SessionPhase.AWAITING_EVENTS
    session.state.pending_events.add(
        PendingRow(
            tool_call_id="child-1",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=7,
            status=RowStatus.PENDING,
        )
    )
    path = tmp_path / "pending.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    row = loaded.state.pending_events.rows["child-1"]
    assert loaded.phase is SessionPhase.AWAITING_EVENTS
    assert row.status is RowStatus.FAILED
    assert "interrupted by session restore" in (row.result or "")
    assert loaded.state.pending_events.is_complete()


def test_scheduler_init_preserves_and_drains_restored_awaiting_phase(tmp_path):
    from opencollab.application.scheduler import LaunchSpec, Scheduler

    agent = FakeAgent()
    original = Session(agent=agent, llm=FakeLLMClient())
    original.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "child-restore",
                    "type": "function",
                    "function": {"name": "spawn_agent", "arguments": "{}"},
                }
            ],
        }
    )
    original.phase = SessionPhase.AWAITING_EVENTS
    original.state.pending_events.add(
        PendingRow(
            tool_call_id="child-restore",
            kind=RowKind.CHILD_AGENT,
            order=0,
            ref=9,
            status=RowStatus.PENDING,
        )
    )
    queued_xml = (
        '<teammate-message teammate_id="A1" summary="restored">\n'
        "queued after child\n"
        "</teammate-message>"
    )
    original.state.queue_pending_user_message(
        {
            "role": "user",
            "content": queued_xml,
            "message_content": "queued after child",
            "from_aid": 1,
            "to_aid": 0,
            "summary": "restored",
        }
    )
    path = tmp_path / "scheduler-resume.json"
    original.save(str(path))

    llm = FakeLLMClient(
        [
            llm_response(content="resumed after failed child"),
            llm_response(content="restored teammate handled"),
            llm_response(content="new turn handled"),
        ]
    )
    resumed = Session(agent=agent, llm=llm)

    class ResumeFactory:
        def create_lead_session(self, **kwargs):
            return resumed

    scheduler = Scheduler(
        session_factory=ResumeFactory(),
        worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(),
    )
    scheduler.create_init_process(LaunchSpec(session_file=str(path)))

    assert resumed.phase is SessionPhase.AWAITING_EVENTS
    assert resumed.state.pending_events.is_complete()
    assert len(scheduler._message_inbox[0]) == 1
    assert run(scheduler.run("new question")) == "new turn handled"
    assert resumed.state.pending_events.is_empty()
    assert resumed.state.pending_user_messages == []
    assert scheduler._message_inbox.get(0) == []
    tool_results = [m for m in resumed.messages if m.get("role") == "tool"]
    assert tool_results[-1]["tool_call_id"] == "child-restore"
    assert "interrupted by session restore" in tool_results[-1]["content"]
    assert any(m.get("role") == "tool" for m in llm.calls[0]["messages"])
    assert llm.calls[1]["messages"][-1]["content"].startswith(queued_xml)
    assert llm.calls[2]["messages"][-1]["content"].startswith("new question")


def test_restore_keeps_pending_messages_from_legacy_structured_snapshot(tmp_path):
    path = tmp_path / "legacy-structured.json"
    path.write_text(
        '{"messages":[{"role":"system","content":"sys"}],'
        '"pending_messages":[{"role":"user","content":"queued"}]}',
        encoding="utf-8",
    )

    loaded = load_session(str(path), agent=FakeAgent(), llm=FakeLLMClient())

    assert loaded.state.pending_user_messages == [
        {"role": "user", "content": "queued"}
    ]


def test_restore_closes_tool_call_from_interrupted_execution_phase(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "lost-call",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                }
            ],
        }
    )
    session.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "interrupted.json"
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.phase is SessionPhase.IDLE
    assert loaded.messages[-1] == {
        "role": "tool",
        "tool_call_id": "lost-call",
        "content": "Tool execution interrupted by session restore.",
    }


def test_scheduler_run_clears_stale_pending_rows_from_interrupted_phase(tmp_path):
    from opencollab.application.scheduler import LaunchSpec, Scheduler

    agent = FakeAgent()
    original = Session(agent=agent, llm=FakeLLMClient())
    original.state.append_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "lost-call",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                }
            ],
        }
    )
    original.state.pending_events.add(
        PendingRow(
            tool_call_id="lost-call",
            kind=RowKind.IMMEDIATE,
            order=0,
            status=RowStatus.DONE,
            result="buffered result",
        )
    )
    original.phase = SessionPhase.EXECUTING_TOOLS
    path = tmp_path / "interrupted-with-row.json"
    original.save(str(path))

    resumed = Session(
        agent=agent,
        llm=FakeLLMClient([llm_response(content="fresh answer")]),
    )

    class ResumeFactory:
        def create_lead_session(self, **kwargs):
            return resumed

    scheduler = Scheduler(
        session_factory=ResumeFactory(),
        worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(),
    )
    scheduler.create_init_process(LaunchSpec(session_file=str(path)))

    async def scenario():
        return await asyncio.wait_for(scheduler.run("new question"), timeout=0.25)

    assert run(scenario()) == "fresh answer"
    assert resumed.state.pending_events.is_empty()


def test_session_accepts_explicit_store():
    class FakeStore:
        def __init__(self):
            self.save_calls = []
            self.load_calls = []
            self.loaded_messages = [{"role": "system", "content": "loaded from fake store"}]

        def save(self, path, messages, *, meta=None):
            self.save_calls.append((path, copy.deepcopy(messages), meta))

        def load_messages(self, path, system_prompt):
            self.load_calls.append((path, system_prompt))
            return copy.deepcopy(self.loaded_messages)

    fake_store = FakeStore()
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient(), store=fake_store)
    session.messages.append({"role": "user", "content": "hello"})

    session.save("fake-session.jsonl")
    loaded = load_session(
        "fake-session.jsonl", agent=agent, llm=FakeLLMClient(), store=fake_store
    )

    assert session.store is fake_store
    assert len(fake_store.save_calls) == 1
    saved_path, saved_messages, _ = fake_store.save_calls[0]
    assert saved_path == "fake-session.jsonl"
    assert [m["role"] for m in saved_messages] == [m["role"] for m in session.messages]
    assert saved_messages[-1]["content"] == "hello"
    assert loaded.store is fake_store
    assert fake_store.load_calls == [("fake-session.jsonl", agent.system_prompt)]
    assert loaded.messages == fake_store.loaded_messages


def test_session_store_preserves_messages_only_jsonl_semantics(tmp_path):
    store = SessionStore()
    path = tmp_path / "stored.jsonl"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]

    store.save(str(path), messages)

    assert store.load_messages(str(path), "fallback") == messages
