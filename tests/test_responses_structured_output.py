"""Request and structured-output tests for the Responses adapter."""

from __future__ import annotations

import json

import pytest
from responses_provider_test_support import (
    FakeStream,
    completed_response,
    message_item,
    ns,
)

from opencollab.adapters.llm.responses_provider import (
    ResponsesProtocolError,
    _build_request_kwargs,
    complete_responses,
)
from opencollab.application.structured_output import StructuredOutputTool


def test_request_maps_instructions_tools_reasoning_and_sampling():
    kwargs = _build_request_kwargs(
        "gpt-5.6-sol",
        [
            {"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "Fix it."},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write one file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    "strict": True,
                },
            }
        ],
        1.0,
        tool_choice={"type": "function", "function": {"name": "write_file"}},
        top_p=0.95,
        max_output_tokens=32768,
        reasoning_effort="xhigh",
    )

    assert kwargs["instructions"] == "You are a coder."
    assert kwargs["input"] == [{"role": "user", "content": "Fix it."}]
    assert kwargs["tools"][0]["name"] == "write_file"
    assert kwargs["tool_choice"] == {"type": "function", "name": "write_file"}
    assert kwargs["reasoning"] == {"effort": "xhigh"}
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["store"] is False
    assert kwargs["stream"] is True
    assert kwargs["temperature"] == 1.0
    assert kwargs["top_p"] == 0.95
    assert kwargs["max_output_tokens"] == 32768


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-flash-0731"])
def test_deepseek_flash_binds_structured_output_with_json_schema(model):
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    kwargs = _build_request_kwargs(
        model,
        [{"role": "user", "content": "Use the tool."}],
        [
            {
                "type": "function",
                "function": {
                    "name": "structured_output",
                    "description": "Return the result.",
                    "parameters": schema,
                },
            }
        ],
        1.0,
        tool_choice={"type": "function", "function": {"name": "structured_output"}},
    )

    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    assert kwargs["text"] == {
        "format": {
            "type": "json_schema",
            "name": "structured_output",
            "description": "Return the result.",
            "schema": schema,
            "strict": True,
        }
    }


@pytest.mark.parametrize("model", ["k3", "kimi-for-coding"])
def test_models_without_verified_json_schema_support_keep_auto_tool_choice(model):
    kwargs = _build_request_kwargs(
        model,
        [{"role": "user", "content": "Use the tool."}],
        [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        1.0,
        tool_choice={"type": "function", "function": {"name": "write_file"}},
    )

    assert kwargs["tool_choice"] == "auto"
    assert "text" not in kwargs


def test_deepseek_flash_auto_multi_tool_request_is_unchanged():
    tools = [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
        for name in ("read_file", "grep")
    ]

    kwargs = _build_request_kwargs(
        "deepseek-v4-flash",
        [{"role": "user", "content": "Inspect the code."}],
        tools,
        1.0,
        tool_choice="auto",
    )

    assert [tool["name"] for tool in kwargs["tools"]] == ["read_file", "grep"]
    assert kwargs["tool_choice"] == "auto"
    assert "text" not in kwargs


def test_deepseek_flash_other_named_single_tool_keeps_legacy_auto_fallback():
    kwargs = _build_request_kwargs(
        "deepseek-v4-flash",
        [{"role": "user", "content": "Submit findings."}],
        [
            {
                "type": "function",
                "function": {
                    "name": "submit_findings",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        1.0,
        tool_choice={
            "type": "function",
            "function": {"name": "submit_findings"},
        },
    )

    assert kwargs["tool_choice"] == "auto"
    assert kwargs["tools"][0]["name"] == "submit_findings"
    assert "text" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_json_schema_text_projects_to_valid_structured_output_tool_call(stream):
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    item = message_item('{"status":"ok"}')
    terminal = completed_response(response_id="resp_schema", output=[item])

    class Responses:
        async def create(self, **kwargs):
            assert kwargs["text"]["format"]["schema"] == schema
            assert "tools" not in kwargs
            assert "tool_choice" not in kwargs
            if not stream:
                return terminal
            return FakeStream(
                [
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(type="response.completed", response=terminal),
                ]
            )

    result = await complete_responses(
        ns(responses=Responses()),
        "deepseek-v4-flash",
        [{"role": "user", "content": "Return the result."}],
        [
            {
                "type": "function",
                "function": {
                    "name": "structured_output",
                    "description": "Return the result.",
                    "parameters": schema,
                },
            }
        ],
        1.0,
        0,
        tool_choice={
            "type": "function",
            "function": {"name": "structured_output"},
        },
        stream=stream,
    )

    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.provider_items[-1]["type"] == "function_call"
    arguments = json.loads(result.tool_calls[0]["function"]["arguments"])
    capture = StructuredOutputTool(schema)
    await capture.execute_with_runtime(arguments, None)  # type: ignore[arg-type]
    assert capture.captured == {"status": "ok"}


@pytest.mark.asyncio
async def test_json_schema_text_rejects_filtered_incomplete_response():
    class Responses:
        async def create(self, **_kwargs):
            return FakeStream(
                [
                    ns(
                        type="response.incomplete",
                        response=completed_response(
                            status="incomplete",
                            incomplete_details={"reason": "content_filter"},
                        ),
                    )
                ]
            )

    with pytest.raises(ResponsesProtocolError, match="content_filter"):
        await complete_responses(
            ns(responses=Responses()),
            "deepseek-v4-flash",
            [{"role": "user", "content": "Return the result."}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "structured_output",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            1.0,
            0,
            tool_choice={
                "type": "function",
                "function": {"name": "structured_output"},
            },
        )
