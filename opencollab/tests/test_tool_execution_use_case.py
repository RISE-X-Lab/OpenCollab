import asyncio
from pathlib import Path
from types import SimpleNamespace

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionState
from opencollab.domain.tools import LoopDetection


def run(coro):
    return asyncio.run(coro)


def tool_call(name: str = "fake_tool", arguments: str = "{}") -> dict:
    return {
        "id": "call-1",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


class FakeAgent:
    def __init__(self, tools=None):
        self.tools = tools or []

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class FakeEventPublisher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class FakeTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, **kwargs):
        self.steps.append(kwargs)


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


class FakeSafetyPolicy:
    pass


class RuntimeNativeTool:
    name = "fake_tool"

    def __init__(self, output: str = "runtime result"):
        self.output = output
        self.runtime_calls = []

    async def execute_with_runtime(self, args, runtime):
        self.runtime_calls.append((args, runtime))
        return self.output


def event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    # Wrap to use SimpleNamespace so tests that previously asserted on a
    # plain object (no aid field) keep their assertion shapes.
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(
            type="loop_detected",
            data={"tool": tool, "count": count},
        ),
        tool_start=lambda tool, args: SimpleNamespace(
            type="tool_start",
            data={"tool": tool, "args": args},
        ),
        tool_end=lambda tool, latency: SimpleNamespace(
            type="tool_end",
            data={"tool": tool, "latency": latency},
        ),
    )


def build_use_case(
    *,
    agent=None,
    state=None,
    event_publisher=None,
    tracer=None,
    environment=None,
    permission_policy=None,
    safety_policy=None,
):
    publisher = event_publisher or FakeEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=agent or FakeAgent(),
        environment=environment,
        state=state or SessionState(messages=[]),
        event_publisher=publisher,
        event_factory=event_factory(),
        tracer=tracer,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
    )
    return use_case, publisher


def test_tool_execution_use_case_preserves_invalid_json_error():
    use_case, publisher = build_use_case()

    result = run(use_case.process([tool_call(arguments="{not-json")]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Error: invalid JSON arguments: {not-json",
        }
    ]
    assert publisher.events == []


def test_tool_execution_use_case_preserves_unknown_tool_error():
    agent = FakeAgent(tools=[SimpleNamespace(name="known_tool")])
    use_case, publisher = build_use_case(agent=agent)

    result = run(use_case.process([tool_call(name="missing_tool", arguments="{}")]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Error: unknown tool 'missing_tool'. Available: ['known_tool']",
        }
    ]
    assert publisher.events == []


def test_tool_execution_use_case_preserves_loop_detection_event():
    state = SessionState(messages=[])
    use_case, publisher = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]
    assert "Loop detected" in result.messages_to_append[0]["content"]
    assert [(event.type, event.data) for event in publisher.events] == [
        ("loop_detected", {"tool": "fake_tool", "count": 3})
    ]


def test_tool_execution_use_case_detects_cyclic_loop_spread_across_window():
    # Regression: real 100-step stalls thrash in a CYCLE — the same (tool, args)
    # recurs every ~13 calls (observed 10-17 apart), not back-to-back, because
    # cleared tool outputs force re-reads. The old detector only scanned the last
    # 6 hashes and never saw three of a cyclically-repeated call, so it never
    # fired. The detector must count across the whole per-turn window.
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    filler = [f"other-{i}" for i in range(12)]  # one full thrash cycle of 13
    # two prior occurrences, each separated by a full cycle -> 13 calls apart
    state.replace_recent_tool_hashes([call_hash, *filler, call_hash, *filler])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]
    assert "Loop detected" in result.messages_to_append[0]["content"]


def test_tool_execution_use_case_executes_runtime_native_tool_and_events():
    tool = RuntimeNativeTool()
    agent = FakeAgent(tools=[tool])
    env = object()
    safety_policy = FakeSafetyPolicy()
    permission_policy = FakePermissionPolicy()
    use_case, publisher = build_use_case(
        agent=agent,
        environment=env,
        safety_policy=safety_policy,
        permission_policy=permission_policy,
    )

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.messages_to_append == [
        {"role": "tool", "tool_call_id": "call-1", "content": "runtime result"}
    ]
    assert len(tool.runtime_calls) == 1
    args, runtime = tool.runtime_calls[0]
    assert args == {"value": 1}
    assert runtime.environment is env
    assert runtime.safety_policy is safety_policy
    assert runtime.permission_policy is permission_policy
    assert publisher.events[0].type == "tool_start"
    assert publisher.events[0].data == {"tool": "fake_tool", "args": {"value": 1}}
    assert publisher.events[1].type == "tool_end"
    assert publisher.events[1].data["tool"] == "fake_tool"


def test_tool_execution_use_case_preserves_trace_payload_capping():
    raw_output = "a" * 10_000
    tool = RuntimeNativeTool(output=raw_output)
    tracer = FakeTracer()
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]), tracer=tracer)

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.messages_to_append[0]["content"] == raw_output
    assert len(tracer.steps) == 1
    payload = tracer.steps[0]["payload"]
    assert payload["tool"] == "fake_tool"
    assert payload["args"] == {"value": 1}
    assert payload["result_len"] == len(raw_output)
    assert "\n...[truncated]...\n" in payload["result"]


def test_tool_execution_use_case_persists_full_tool_output():
    # The full result is appended/persisted; bounding what the model sees is the
    # job of the call-time per-tool-result budget shaper, not tool execution.
    raw_output = "a" * 50_000
    tool = RuntimeNativeTool(output=raw_output)
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]))

    result = run(use_case.process([tool_call()]))

    content = result.messages_to_append[0]["content"]
    assert content == raw_output


def test_short_circuit_invalid_json_is_traced():
    tracer = FakeTracer()
    use_case, _ = build_use_case(tracer=tracer)

    run(use_case.process([tool_call(arguments="{not-json")]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "tool_error"
    assert step["payload"]["tool"] == "fake_tool"
    assert step["payload"]["error"] == "invalid_json_args"


def test_short_circuit_unknown_tool_is_traced():
    tracer = FakeTracer()
    agent = FakeAgent(tools=[SimpleNamespace(name="known_tool")])
    use_case, _ = build_use_case(agent=agent, tracer=tracer)

    run(use_case.process([tool_call(name="missing_tool", arguments="{}")]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "tool_error"
    assert step["payload"]["tool"] == "missing_tool"
    assert step["payload"]["error"] == "unknown_tool"


def test_short_circuit_loop_block_is_traced():
    tracer = FakeTracer()
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state, tracer=tracer)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "loop_blocked"
    assert step["payload"]["tool"] == "fake_tool"
    assert step["payload"]["count"] == 3


def test_application_tool_execution_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/tool_execution.py").read_text(encoding="utf-8")

    assert "opencollab.core.session" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tui" not in source
