import asyncio
from pathlib import Path
from types import SimpleNamespace

from opencollab.application.tool_execution import (
    MAX_TOOL_OUTPUT_CHARS,
    ToolExecutionEventFactory,
    ToolExecutionUseCase,
)
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


def event_factory() -> ToolExecutionEventFactory:
    return ToolExecutionEventFactory(
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


def test_tool_execution_use_case_preserves_tool_output_truncation():
    raw_output = "a" * (MAX_TOOL_OUTPUT_CHARS + 10)
    tool = RuntimeNativeTool(output=raw_output)
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]))

    result = run(use_case.process([tool_call()]))

    content = result.messages_to_append[0]["content"]
    assert len(content) > MAX_TOOL_OUTPUT_CHARS
    assert "... [10 chars truncated] ..." in content


def test_application_tool_execution_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/tool_execution.py").read_text(encoding="utf-8")

    assert "opencollab.core.session" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tui" not in source
