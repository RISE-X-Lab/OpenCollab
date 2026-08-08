"""Observability and bounded-payload contracts for tool execution."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from tool_execution_test_support import FakeAgent
from tool_execution_test_support import RecordingEventPublisher as FakeEventPublisher

import opencollab.application.tool_execution_runtime as tool_execution_runtime
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


def tool_call(
    name: str = "fake_tool",
    arguments: object = "{}",
    call_id: str = "call-1",
) -> dict:
    return {
        "id": call_id,
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


class FakeTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, **kwargs):
        self.steps.append(kwargs)


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
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(
            type="loop_detected",
            data={"tool": tool, "count": count},
        ),
        tool_start=lambda tool, args, tool_call_id: SimpleNamespace(
            type="tool_start",
            data={"tool": tool, "args": args, "tool_call_id": tool_call_id},
        ),
        tool_end=lambda tool, latency, tool_call_id: SimpleNamespace(
            type="tool_end",
            data={"tool": tool, "latency": latency, "tool_call_id": tool_call_id},
        ),
    )


def build_use_case(*, agent=None, state=None, event_publisher=None, tracer=None):
    publisher = event_publisher or FakeEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=agent or FakeAgent(),
        environment=None,
        state=state or SessionState(messages=[]),
        event_publisher=publisher,
        event_factory=event_factory(),
        tracer=tracer,
    )
    return use_case, publisher


def test_tool_trace_failure_does_not_discard_executed_result():
    class FailingTracer:
        def log_step(self, **kwargs):
            raise RuntimeError("trace failed")

    tool = RuntimeNativeTool()
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[tool]),
        tracer=FailingTracer(),
    )
    result = run(use_case.process([tool_call()]))
    assert result.messages_to_append[0]["content"] == "runtime result"


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
    assert payload["tool_call_id"] == "call-1"
    assert payload["args"] == {"value": 1}
    assert payload["result_len"] == len(raw_output)
    assert "\n...[truncated]...\n" in payload["result"]


def test_tool_observations_bound_large_nested_arguments():
    content = "x" * (4 * 1024 * 1024)
    arguments = {"content": content, "nested": {"small": "kept"}}
    tool = RuntimeNativeTool()
    tracer = FakeTracer()
    use_case, publisher = build_use_case(
        agent=FakeAgent(tools=[tool]),
        tracer=tracer,
    )
    run(use_case.process([tool_call(arguments=json.dumps(arguments))]))

    event_args = publisher.events[0].data["args"]
    trace_args = tracer.steps[0]["payload"]["args"]
    assert event_args == trace_args
    assert len(json.dumps(event_args).encode("utf-8")) < 16 * 1024
    assert content not in json.dumps(event_args)
    assert event_args["content"] == {
        "__opencollab_truncated__": True,
        "preview": "x" * 512,
        "original_length": len(content),
        "original_bytes": len(content),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    assert event_args["nested"] == {"small": "kept"}
    assert tool.runtime_calls[0][0] == arguments


def test_tool_observation_budget_accounts_for_large_numeric_scalars():
    huge_integer = int("9" * 4000)
    arguments = {"values": [huge_integer] * 64}
    tool = RuntimeNativeTool()
    tracer = FakeTracer()
    use_case, publisher = build_use_case(
        agent=FakeAgent(tools=[tool]),
        tracer=tracer,
    )
    run(use_case.process([tool_call(arguments=json.dumps(arguments))]))

    event_args = publisher.events[0].data["args"]
    trace_args = tracer.steps[0]["payload"]["args"]
    serialized = json.dumps(event_args, ensure_ascii=False).encode("utf-8")
    assert event_args == trace_args
    assert len(serialized) <= tool_execution_runtime.OBSERVATION_PAYLOAD_BUDGET_BYTES
    assert any(
        isinstance(value, dict)
        and value.get("__opencollab_truncated__") is True
        and value.get("original_type") == "int"
        for value in event_args["values"]
    )
    assert tool.runtime_calls[0][0] == arguments


def test_tool_execution_use_case_persists_full_tool_output():
    raw_output = "a" * 50_000
    tool = RuntimeNativeTool(output=raw_output)
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]))
    result = run(use_case.process([tool_call()]))
    assert result.messages_to_append[0]["content"] == raw_output
