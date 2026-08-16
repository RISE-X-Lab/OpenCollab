"""Cross-provider request, response, and error contract tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from responses_provider_test_support import FakeStream, completed_response, message_item, ns

from opencollab.adapters.llm.anthropic_provider import (
    _build_request_kwargs as build_anthropic_kwargs,
)
from opencollab.adapters.llm.anthropic_provider import (
    _parse_response as parse_anthropic_response,
)
from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.adapters.llm.openai_provider import (
    _build_request_kwargs as build_openai_kwargs,
)
from opencollab.adapters.llm.openai_provider import (
    _parse_response as parse_openai_response,
)
from opencollab.adapters.llm.responses_provider import (
    ResponsesProtocolError,
    _consume_stream,
    _messages_to_input,
    _parse_stream,
)
from opencollab.adapters.llm.responses_provider import (
    _build_request_kwargs as build_responses_kwargs,
)


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize("model", ["o4-mini", "gpt-5", "gateway/gpt-5.6-sol"])
def test_chat_reasoning_models_use_reasoning_request_fields(model):
    kwargs = build_openai_kwargs(
        model,
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        top_p=0.9,
        max_output_tokens=321,
        reasoning_effort="max",
    )

    assert kwargs["reasoning_effort"] == "max"
    assert kwargs["max_completion_tokens"] == 321
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


def test_chat_reasoning_effort_is_forwarded_for_compatible_models():
    kwargs = build_openai_kwargs(
        "deepseek-v4-flash-0731",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        reasoning_effort="max",
    )

    assert kwargs["reasoning_effort"] == "max"


def test_chat_thinking_params_cannot_override_reasoning_effort():
    with pytest.raises(ValueError, match="reasoning_effort"):
        build_openai_kwargs(
            "deepseek-v4-flash",
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            thinking=True,
            thinking_params={"reasoning_effort": "low"},
            reasoning_effort="max",
        )


def test_anthropic_effort_works_without_explicit_thinking():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-5",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        reasoning_effort="max",
    )

    assert kwargs["output_config"] == {"effort": "max"}
    assert "temperature" not in kwargs
    assert "thinking" not in kwargs


def test_anthropic_default_on_model_honors_thinking_false():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-5",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        thinking=False,
    )

    assert kwargs["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    "model",
    ["claude-fable-5", "claude-mythos-5", "claude-mythos-preview"],
)
def test_anthropic_always_on_models_reject_thinking_false(model):
    with pytest.raises(ValueError, match="cannot disable adaptive thinking"):
        build_anthropic_kwargs(
            model,
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            thinking=False,
        )


def test_anthropic_mythos_preview_still_accepts_manual_thinking():
    kwargs = build_anthropic_kwargs(
        "claude-mythos-preview",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        thinking=True,
        thinking_params={"thinking": {"type": "enabled", "budget_tokens": 4096}},
        max_output_tokens=8192,
    )

    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_anthropic_modern_models_omit_sampling_without_explicit_thinking():
    kwargs = build_anthropic_kwargs(
        "gateway/claude-opus-4-8-20260801",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        top_p=1.0,
    )

    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


def test_anthropic_modern_models_reject_non_default_top_p():
    with pytest.raises(ValueError, match="provider-default top_p"):
        build_anthropic_kwargs(
            "claude-sonnet-5",
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            top_p=0.9,
        )


@pytest.mark.parametrize(
    "model",
    ["claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5"],
)
def test_anthropic_adaptive_only_models_reject_manual_thinking(model):
    with pytest.raises(ValueError, match="requires adaptive thinking"):
        build_anthropic_kwargs(
            model,
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            thinking=True,
            thinking_params={"thinking": {"type": "enabled", "budget_tokens": 4096}},
            max_output_tokens=8192,
        )


@pytest.mark.parametrize(
    "model",
    ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
)
def test_anthropic_manual_only_models_reject_adaptive_thinking(model):
    with pytest.raises(ValueError, match="does not support adaptive thinking"):
        build_anthropic_kwargs(
            model,
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            thinking=True,
            thinking_params={"thinking": {"type": "adaptive"}},
        )


def test_anthropic_effort_merges_with_native_output_format():
    schema = _schema()
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-5",
        [{"role": "user", "content": "solve"}],
        None,
        0.2,
        thinking=True,
        thinking_params={
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": "max",
                "format": {"type": "json_schema", "schema": schema},
            },
        },
        reasoning_effort="max",
    )

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {
        "effort": "max",
        "format": {"type": "json_schema", "schema": schema},
    }


def test_anthropic_rejects_conflicting_effort_sources():
    with pytest.raises(ValueError, match="conflicts"):
        build_anthropic_kwargs(
            "claude-sonnet-5",
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            thinking=True,
            thinking_params={
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "low"},
            },
            reasoning_effort="max",
        )


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_anthropic_rejects_unsupported_generic_effort(effort):
    with pytest.raises(ValueError, match="unsupported Anthropic reasoning_effort"):
        build_anthropic_kwargs(
            "claude-sonnet-5",
            [{"role": "user", "content": "solve"}],
            None,
            0.2,
            reasoning_effort=effort,
        )


def test_anthropic_preserves_strict_tool_contract():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-5",
        [{"role": "user", "content": "use the tool"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "submit",
                    "description": "Submit one value.",
                    "parameters": _schema(),
                    "strict": True,
                },
            }
        ],
        0.2,
    )

    assert kwargs["tools"] == [
        {
            "name": "submit",
            "description": "Submit one value.",
            "input_schema": _schema(),
            "strict": True,
        }
    ]


def _function_tool(*, strict: object = True, parameters: object = None) -> dict:
    function: dict[str, object] = {
        "name": "submit",
        "description": "Submit one value.",
        "parameters": _schema() if parameters is None else parameters,
        "strict": strict,
    }
    return {"type": "function", "function": function}


def test_all_provider_paths_preserve_false_strict_and_empty_schema():
    tool = _function_tool(strict=False, parameters={})
    messages = [{"role": "user", "content": "submit"}]

    chat = build_openai_kwargs("gpt-4o", messages, [tool], 0.2)
    anthropic = build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2)
    responses = build_responses_kwargs("gpt-5", messages, [tool], 0.2)

    assert chat["tools"][0]["function"]["strict"] is False
    assert chat["tools"][0]["function"]["parameters"] == {}
    assert anthropic["tools"][0]["strict"] is False
    assert anthropic["tools"][0]["input_schema"] == {}
    assert responses["tools"][0]["strict"] is False
    assert responses["tools"][0]["parameters"] == {}


@pytest.mark.parametrize("strict", ["false", 0, 1, None])
def test_all_provider_paths_reject_non_boolean_strict(strict):
    tool = _function_tool(strict=strict)
    messages = [{"role": "user", "content": "submit"}]

    with pytest.raises(ValueError, match="strict must be a boolean"):
        build_openai_kwargs("gpt-4o", messages, [tool], 0.2)
    with pytest.raises(ValueError, match="strict must be a boolean"):
        build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2)
    with pytest.raises(ResponsesProtocolError, match="strict must be a boolean"):
        build_responses_kwargs("gpt-5", messages, [tool], 0.2)


@pytest.mark.parametrize("name", ["has space", "é", "x" * 65])
def test_all_provider_paths_reject_non_portable_tool_names(name):
    tool = _function_tool()
    tool["function"]["name"] = name
    messages = [{"role": "user", "content": "submit"}]

    with pytest.raises(ValueError, match="1-64"):
        build_openai_kwargs("gpt-4o", messages, [tool], 0.2)
    with pytest.raises(ValueError, match="1-64"):
        build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2)
    with pytest.raises(ResponsesProtocolError, match="1-64"):
        build_responses_kwargs("gpt-5", messages, [tool], 0.2)


def test_named_tool_choice_has_one_cross_provider_meaning():
    tool = _function_tool()
    messages = [{"role": "user", "content": "submit"}]
    choice = {"type": "function", "name": "submit"}

    chat = build_openai_kwargs("gpt-4o", messages, [tool], 0.2, tool_choice=choice)
    anthropic = build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2, tool_choice=choice)
    responses = build_responses_kwargs("gpt-5", messages, [tool], 0.2, tool_choice=choice)

    assert chat["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit"},
    }
    assert anthropic["tool_choice"] == {"type": "tool", "name": "submit"}
    assert responses["tool_choice"] == {"type": "function", "name": "submit"}


@pytest.mark.parametrize(
    "choice",
    [
        "sometimes",
        {"type": "function", "function": {}},
        {"type": "function", "name": "submit", "unexpected": True},
        {"type": "unknown"},
    ],
)
def test_all_provider_paths_reject_malformed_tool_choice(choice):
    tool = _function_tool()
    messages = [{"role": "user", "content": "submit"}]

    with pytest.raises(ValueError, match="tool_choice"):
        build_openai_kwargs("gpt-4o", messages, [tool], 0.2, tool_choice=choice)
    with pytest.raises(ValueError, match="tool_choice"):
        build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2, tool_choice=choice)
    with pytest.raises(ResponsesProtocolError, match="tool_choice"):
        build_responses_kwargs("gpt-5", messages, [tool], 0.2, tool_choice=choice)


def test_all_provider_paths_reject_unavailable_named_tool():
    tool = _function_tool()
    messages = [{"role": "user", "content": "submit"}]
    choice = {"type": "function", "function": {"name": "missing"}}

    with pytest.raises(ValueError, match="unavailable tool"):
        build_openai_kwargs("gpt-4o", messages, [tool], 0.2, tool_choice=choice)
    with pytest.raises(ValueError, match="unavailable tool"):
        build_anthropic_kwargs("claude-sonnet-5", messages, [tool], 0.2, tool_choice=choice)
    with pytest.raises(ResponsesProtocolError, match="unavailable tool"):
        build_responses_kwargs("gpt-5", messages, [tool], 0.2, tool_choice=choice)


def test_responses_keeps_compaction_record_in_conversation_order():
    instructions, items = _messages_to_input(
        [
            {"role": "system", "content": "global identity"},
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "system",
                "content": "[Context auto-compacted]: earlier work",
                "compacted": True,
            },
            {"role": "user", "content": "new request"},
        ]
    )

    assert instructions == "global identity"
    assert items == [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "[Context auto-compacted]: earlier work"},
        {"role": "user", "content": "new request"},
    ]


def test_responses_rejects_unsupported_content_instead_of_dropping_it():
    with pytest.raises(ResponsesProtocolError, match="unsupported type 'image_url'"):
        _messages_to_input(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/image.png"},
                        },
                    ],
                }
            ]
        )


def test_responses_converts_image_url_instead_of_dropping_it():
    _, items = _messages_to_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.test/image.png",
                            "detail": "high",
                        },
                    },
                ],
            }
        ]
    )

    assert items == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "inspect"},
                {
                    "type": "input_image",
                    "image_url": "https://example.test/image.png",
                    "detail": "high",
                },
            ],
        }
    ]


def test_anthropic_rejects_unknown_message_role_instead_of_dropping_it():
    with pytest.raises(ValueError, match="unsupported role 'developer'"):
        build_anthropic_kwargs(
            "claude-sonnet-5",
            [{"role": "developer", "content": "hidden instruction"}],
            None,
            0.2,
        )


@pytest.mark.asyncio
async def test_responses_typed_context_overflow_keeps_machine_identity():
    event = ns(
        type="response.failed",
        response=ns(
            error={
                "code": "context_length_exceeded",
                "message": "request exceeds the context window",
            }
        ),
    )

    with pytest.raises(ResponsesProtocolError) as captured:
        await _consume_stream(FakeStream([event]), 1, 1, "gpt-fake")

    assert is_context_overflow_error(captured.value) is True
    assert captured.value.code == "context_length_exceeded"
    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_responses_max_token_terminal_preserves_partial_output():
    item = message_item("partial answer")
    response = completed_response(
        status="incomplete",
        incomplete_details={"reason": "max_tokens"},
        output=[item],
    )
    state = await _consume_stream(
        FakeStream(
            [
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.incomplete", response=response),
            ]
        ),
        1,
        1,
        "gpt-fake",
    )

    parsed = _parse_stream(
        state,
        [{"role": "user", "content": "solve"}],
        "gpt-fake",
    )

    assert parsed.content == "partial answer"
    assert parsed.finish_reason == "max_tokens"


def test_chat_response_records_reasoning_usage_and_provider_model():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    message = SimpleNamespace(content="answer", tool_calls=None, reasoning_content="work")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
        model="provider/gpt-5.6-sol-2026-08-07",
    )

    parsed = parse_openai_response(response, [{"role": "user", "content": "solve"}])

    assert parsed.usage.reasoning_tokens == 7
    assert parsed.provider_model == "provider/gpt-5.6-sol-2026-08-07"


def test_anthropic_response_records_reasoning_usage_and_provider_model():
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=40,
        cache_creation_input_tokens=10,
        output_tokens_details=SimpleNamespace(thinking_tokens=7),
    )
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="answer")],
        usage=usage,
        stop_reason="end_turn",
        model="claude-sonnet-5-20260801",
    )

    parsed = parse_anthropic_response(response)

    assert parsed.usage.reasoning_tokens == 7
    assert parsed.provider_model == "claude-sonnet-5-20260801"
