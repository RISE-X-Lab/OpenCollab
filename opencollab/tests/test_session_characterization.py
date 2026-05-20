import asyncio
import copy

import pytest

from opencollab.core import session as session_mod
from opencollab.core.events import EventBus as CompatEventBus
from opencollab.core.events import SessionEvent as CompatSessionEvent
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.core.session import (
    CallbackPermissionPolicy,
    EventBus,
    Session,
    SessionEvent,
    SessionPhase,
)
from opencollab.adapters.storage import SessionStore


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


def fake_team_session_factory(*, safety_policy_factory=None, repo_map=None):
    from opencollab.bootstrap.teammate_factory import DefaultSessionFactory, TeammateConfig

    event_bus = EventBus()
    factory = DefaultSessionFactory(
        TeammateConfig(
            model="fake-model",
            provider="fake-provider",
            api_key="fake-key",
            base_url=None,
            tracer=None,
            event_bus=event_bus,
            permission_policy=None,
            repo_map=repo_map,
            safety_policy_factory=safety_policy_factory,
        )
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


def test_session_package_and_compat_event_imports_are_preserved():
    assert Session is session_mod.Session
    assert SessionEvent is session_mod.SessionEvent
    assert CompatEventBus is EventBus
    assert CompatSessionEvent is SessionEvent


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

        def save(self, path, messages):
            self.save_calls.append((path, copy.deepcopy(messages)))

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
    assert store.save_calls == [("autosave.jsonl", session.messages)]


def test_snapshot_preserves_historical_subset_only():
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.messages.append({"role": "assistant", "content": "old answer"})
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    session.phase = SessionPhase.DONE
    session._recent_call_hashes.append("hash-1")

    snap = session.snapshot()

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
    assert [(event.type, event.data) for event in events] == [("error", {"reason": "cancelled"})]


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
    assert [(event.type, event.data) for event in events] == [("error", {"reason": "budget_exceeded"})]


def test_compaction_trigger_summarizes_older_messages_then_runs_step():
    fake_llm = FakeLLMClient([
        llm_response(content="compact summary", input_tokens=3, output_tokens=4),
        llm_response(content="final after compaction", input_tokens=5, output_tokens=6),
    ])
    tracer = FakeTracer()
    events, on_event = event_collector()
    session = Session(
        agent=FakeAgent(),
        llm=fake_llm,
        tracer=tracer,
        compaction_threshold=0,
        event_sink=EventBus(on_event),
    )
    session.messages.extend({"role": "user", "content": f"message {idx}"} for idx in range(10))
    original_recent = copy.deepcopy(session.messages[-session_mod.COMPACTION_KEEP_RECENT:])

    result = run(session.run_loop())

    assert result == "final after compaction"
    assert fake_llm.calls[0]["temperature"] == 0.0
    assert fake_llm.calls[0]["messages"][0]["content"].startswith("You are a context compaction assistant.")
    assert fake_llm.calls[1]["messages"][1]["content"] == (
        "[Context compacted — summary of 2 earlier messages]:\ncompact summary"
    )
    assert session.messages[0] == {"role": "system", "content": "You are a fake agent."}
    assert session.messages[2:10] == original_recent
    assert session.used_tokens == 18
    assert "compaction" in [event.type for event in events]
    assert tracer.steps[0]["step_type"] == "compaction"


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
    assert session.compactor.llm is fake_llm

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
    assert session.runner.event_bus is session.event_bus
    assert session.tool_processor.event_bus is session.event_bus
    assert session.compactor.event_bus is session.event_bus

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
    assert session.phase == SessionPhase.IDLE


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

    result = run(session.tool_processor.process([tool_call(arguments='{"value": 9}')]))

    assert isinstance(result, session_mod.ToolProcessingResult)
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
        ("loop_detected", {"tool": "fake_tool", "count": 3})
    ]


def test_tool_output_is_truncated_before_appending_to_messages():
    long_result = (
        "a" * (session_mod.MAX_TOOL_OUTPUT_CHARS // 2)
        + "b" * 123
        + "c" * (session_mod.MAX_TOOL_OUTPUT_CHARS // 2)
    )
    tool = FakeTool(result=long_result)
    fake_llm = FakeLLMClient([
        llm_response(tool_calls=[tool_call(arguments='{"value": 1}')], finish_reason="tool_calls"),
        llm_response(content="after truncation"),
    ])
    session = Session(agent=FakeAgent(tools=[tool]), llm=fake_llm)

    result = run(session.run_loop())

    assert result == "after truncation"
    tool_output = session.messages[2]["content"]
    assert tool_output != long_result
    assert tool_output.startswith("a" * (session_mod.MAX_TOOL_OUTPUT_CHARS // 2))
    assert "\n\n... [123 chars truncated] ...\n\n" in tool_output
    assert tool_output.endswith("c" * (session_mod.MAX_TOOL_OUTPUT_CHARS // 2))


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


def test_context_compactor_should_compact_uses_estimated_messages():
    session = Session(agent=FakeAgent(), llm=FakeLLMClient(), compaction_threshold=0)

    assert session.compactor.should_compact() is True


def test_context_compactor_with_insufficient_messages_only_emits_event():
    fake_llm = FakeLLMClient()
    events, on_event = event_collector()
    session = Session(agent=FakeAgent(), llm=fake_llm, event_sink=EventBus(on_event))
    original_messages = copy.deepcopy(session.messages)

    run(session.compactor.compact())

    assert fake_llm.calls == []
    assert session.messages == original_messages
    assert session.used_tokens == 0
    assert [(event.type, event.data) for event in events] == [
        ("compaction", {"reason": "context_overflow"})
    ]


def test_context_compactor_falls_back_to_raw_text_when_llm_fails():
    fake_llm = FakeLLMClient([RuntimeError("summary failed")])
    session = Session(agent=FakeAgent(), llm=fake_llm)
    session.messages.extend({"role": "user", "content": f"message {idx}"} for idx in range(10))

    run(session.compactor.compact())

    assert len(fake_llm.calls) == 1
    assert session.used_tokens == 0
    assert session.messages[1]["role"] == "system"
    assert session.messages[1]["content"].startswith("[Context compacted — summary of 2 earlier messages]:")
    assert "[user]: message 0" in session.messages[1]["content"]
    assert "[user]: message 1" in session.messages[1]["content"]


def test_context_compactor_can_return_result_before_state_application():
    fake_llm = FakeLLMClient([
        llm_response(content="compact summary", input_tokens=3, output_tokens=4),
    ])
    session = Session(agent=FakeAgent(), llm=fake_llm)
    session.messages.extend({"role": "user", "content": f"message {idx}"} for idx in range(10))
    original_messages = copy.deepcopy(session.messages)

    result = run(session.compactor.compact(apply=False))

    assert isinstance(result, session_mod.CompactResult)
    assert result.did_compact is True
    assert result.compacted_count == 2
    assert result.summary_len == len("compact summary")
    assert result.used_tokens_delta == 7
    assert session.messages == original_messages
    assert session.used_tokens == 0

    result.apply_to(session.state)

    assert session.messages != original_messages
    assert session.messages[1]["content"] == "[Context compacted — summary of 2 earlier messages]:\ncompact summary"
    assert session.used_tokens == 7


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
    assert session.tool_processor.env is old_env
    assert session.runner.max_steps == old_max_steps


def test_team_lead_session_runtime_uses_constructor_env_and_max_steps(monkeypatch):
    from opencollab.bootstrap import container as session_module
    from opencollab.application.team import Team

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())

    lead_env = object()
    lead_max_steps = 7
    event_bus, session_factory = fake_team_session_factory()

    team = Team(
        workspace=".",
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        lead_env=lead_env,
        lead_tools=[],
        lead_max_steps=lead_max_steps,
        use_worktrees=False,
        event_bus=event_bus,
        worktree_pool=fake_worktree_pool(),
        session_factory=session_factory,
    )

    assert team.lead_session.env is lead_env
    assert team.lead_session.tool_processor.env is lead_env
    assert team.lead_session.max_steps == lead_max_steps
    assert team.lead_session.runner.max_steps == lead_max_steps


def test_team_lead_session_gets_workspace_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap.safety import build_workspace_safety_policy
    from opencollab.bootstrap import container as session_module
    from opencollab.application.team import Team
    from opencollab.adapters.safety import SandboxInterceptor
    from opencollab.adapters.env import LocalEnvironment

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory(
        safety_policy_factory=build_workspace_safety_policy,
    )

    team = Team(
        workspace=str(tmp_path),
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        lead_env=LocalEnvironment(str(tmp_path)),
        lead_tools=[],
        use_worktrees=False,
        safety_policy_factory=build_workspace_safety_policy,
        event_bus=event_bus,
        worktree_pool=fake_worktree_pool(),
        session_factory=session_factory,
    )

    policy = team.lead_session.tool_processor.safety_policy
    assert isinstance(policy, SandboxInterceptor)
    assert policy.root == str(tmp_path.resolve())


def test_direct_team_without_safety_factory_does_not_build_safety_policy(tmp_path, monkeypatch):
    from opencollab.bootstrap import container as session_module
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.application.team import Team

    monkeypatch.setattr(session_module, "LLMClient", lambda **kwargs: FakeLLMClient())
    event_bus, session_factory = fake_team_session_factory()

    team = Team(
        workspace=str(tmp_path),
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        lead_env=LocalEnvironment(str(tmp_path)),
        lead_tools=[],
        use_worktrees=False,
        event_bus=event_bus,
        worktree_pool=fake_worktree_pool(),
        session_factory=session_factory,
    )

    assert team.lead_session.tool_processor.safety_policy is None


def test_save_and_load_round_trip_only_messages(tmp_path):
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient())
    session.messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    session.used_tokens = 123
    session.step_count = 4
    session.is_done = True
    path = tmp_path / "session.jsonl"

    session.save(str(path))
    loaded = Session.load(str(path), agent=agent, llm=FakeLLMClient())

    assert loaded.messages == session.messages
    assert loaded.used_tokens == 0
    assert loaded.step_count == 0
    assert loaded.is_done is False


def test_session_accepts_explicit_store():
    class FakeStore:
        def __init__(self):
            self.save_calls = []
            self.load_calls = []
            self.loaded_messages = [{"role": "system", "content": "loaded from fake store"}]

        def save(self, path, messages):
            self.save_calls.append((path, copy.deepcopy(messages)))

        def load_messages(self, path, system_prompt):
            self.load_calls.append((path, system_prompt))
            return copy.deepcopy(self.loaded_messages)

    fake_store = FakeStore()
    agent = FakeAgent()
    session = Session(agent=agent, llm=FakeLLMClient(), store=fake_store)
    session.messages.append({"role": "user", "content": "hello"})

    session.save("fake-session.jsonl")
    loaded = Session.load(
        "fake-session.jsonl", agent=agent, llm=FakeLLMClient(), store=fake_store
    )

    assert session.store is fake_store
    assert fake_store.save_calls == [("fake-session.jsonl", session.messages)]
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
