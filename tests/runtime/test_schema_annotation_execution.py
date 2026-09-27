"""Execution coverage for accepted JSON Schema annotations."""

import asyncio

import pytest

from opencollab.application.events import default_session_event_factory
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.session import SessionState
from tests.support.tool_execution_test_support import FakeAgent, RecordingEventPublisher, RuntimeNativeTool, tool_call


def run(coro):
    return asyncio.run(coro)


def build_use_case(tool: RuntimeNativeTool) -> ToolExecutionUseCase:
    return ToolExecutionUseCase(
        agent=FakeAgent(tools=[tool]),
        environment=None,
        state=SessionState(messages=[]),
        event_publisher=RecordingEventPublisher(),
        event_factory=default_session_event_factory(aid=-1),
    )


def test_declared_schema_annotations_allow_single_and_batch_execution():
    tool = RuntimeNativeTool()
    tool.parameters = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "title": "Third-party tool",
        "description": "Schema annotations should remain compatible",
        "required": ["value"],
        "properties": {
            "value": {
                "type": "string",
                "default": "fallback",
                "examples": ["first", "second"],
                "format": "hostname",
                "deprecated": False,
                "readOnly": False,
                "writeOnly": False,
            }
        },
    }
    use_case = build_use_case(tool)

    result = run(use_case.process([
        tool_call(arguments='{"value": "one"}', call_id="call-1"),
        tool_call(arguments='{"value": "two"}', call_id="call-2"),
    ]))

    assert [call[0] for call in tool.runtime_calls] == [
        {"value": "one"},
        {"value": "two"},
    ]
    assert [message["content"] for message in result.messages_to_append] == [
        "runtime result",
        "runtime result",
    ]


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("anyOf", [{"type": "string"}]),
        ("allOf", [{"type": "string"}]),
        ("const", "fixed"),
        ("$ref", "#/$defs/value"),
        ("nullable", True),
    ],
)
def test_unimplemented_schema_semantics_still_block_execution(keyword, value):
    tool = RuntimeNativeTool()
    tool.parameters = {"type": "object", keyword: value}
    use_case = build_use_case(tool)

    result = run(use_case.process([tool_call()]))

    assert tool.runtime_calls == []
    assert f"{keyword}: unsupported schema keyword" in (
        result.messages_to_append[0]["content"]
    )
