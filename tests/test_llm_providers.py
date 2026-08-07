"""Unit tests for shared provider classification + near-miss warning."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.anthropic_provider import _build_request_kwargs as build_anthropic_kwargs
from opencollab.adapters.llm.anthropic_provider import _parse_response as parse_anthropic_response
from opencollab.adapters.llm.anthropic_provider import _parse_usage as parse_anthropic_usage
from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.adapters.llm.openai_provider import _build_request_kwargs as build_openai_kwargs
from opencollab.adapters.llm.openai_provider import _parse_response as parse_openai_response
from opencollab.adapters.llm.openai_provider import _usage_int as openai_usage_int
from opencollab.adapters.llm.providers import (
    is_anthropic,
    normalize_provider,
    required_env_key,
    warn_provider_near_miss,
)
from opencollab.adapters.llm.types import estimate_messages_tokens, model_capabilities


@pytest.mark.parametrize("provider", ["anthropic", "Anthropic", "  ANTHROPIC  "])
def test_is_anthropic_normalizes_case_and_whitespace(provider):
    assert is_anthropic(provider) is True


@pytest.mark.parametrize("provider", ["openai", "deepseek", "claude", None])
def test_is_anthropic_false_for_non_anthropic(provider):
    assert is_anthropic(provider) is False


def test_normalize_provider_defaults_to_openai_only_for_none():
    assert normalize_provider(None) == "openai"
    # Whitespace strips to empty, which is still a non-anthropic provider.
    assert normalize_provider("  ") == ""
    assert is_anthropic("  ") is False


def test_required_env_key_maps_provider_to_key():
    assert required_env_key("anthropic") == "ANTHROPIC_API_KEY"
    assert required_env_key("openai") == "OPENAI_API_KEY"
    assert required_env_key(None) == "OPENAI_API_KEY"


@pytest.mark.parametrize("provider", ["claude", "Anthropics", "claude-ai", "anthropic-ai"])
def test_near_miss_warns_for_anthropic_lookalikes(caplog, provider):
    with caplog.at_level(logging.WARNING, logger="opencollab.adapters.llm.providers"):
        warn_provider_near_miss(provider)
    assert any("not recognized as 'anthropic'" in r.message for r in caplog.records)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "deepseek", "ollama", None])
def test_near_miss_silent_for_real_providers(caplog, provider):
    with caplog.at_level(logging.WARNING, logger="opencollab.adapters.llm.providers"):
        warn_provider_near_miss(provider)
    assert caplog.records == []


# ---------------------------------------------------------------------------
# Issue A — Anthropic prompt-cache token accounting
# ---------------------------------------------------------------------------


def test_anthropic_folds_cache_tokens_into_input():
    """cache_read + cache_creation must be added to accounted input tokens.

    Anthropic's ``input_tokens`` excludes cached portions, so the true input
    the model processed is input + cache_read + cache_creation.
    """
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=300,
    )
    result = parse_anthropic_usage(usage)

    assert result.input_tokens == 100 + 500 + 300  # 900
    assert result.output_tokens == 20
    assert result.total_tokens == 920  # what add_used_tokens accumulates
    # Cache sub-fields retained for observability (already folded into input).
    assert result.cache_read_tokens == 500
    assert result.cache_creation_tokens == 300


def test_anthropic_without_cache_attrs_does_not_crash():
    """A usage object lacking cache attributes equals plain input+output."""
    usage = SimpleNamespace(input_tokens=100, output_tokens=20)
    result = parse_anthropic_usage(usage)

    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.total_tokens == 120
    assert result.cache_read_tokens == 0
    assert result.cache_creation_tokens == 0


def test_anthropic_treats_none_cache_fields_as_zero():
    """SDK may expose cache attrs set to None when caching is inactive."""
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    result = parse_anthropic_usage(usage)

    assert result.input_tokens == 100
    assert result.total_tokens == 120


# ---------------------------------------------------------------------------
# Issue B — OpenAI-compatible usage-missing estimate fallback
# ---------------------------------------------------------------------------


def _openai_resp(usage, content="hello world", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage)


def test_openai_usage_none_yields_nonzero_estimate():
    """When usage is missing the budget must still advance (non-zero estimate)."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Tell me a long story about token budgets " * 5},
    ]
    resp = _openai_resp(usage=None, content="Once upon a time " * 10)

    result = parse_openai_response(resp, messages)

    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert result.usage.total_tokens > 0  # budget cannot be defeated
    assert result.usage.estimated is True
    # Input estimate is derived from the request messages.
    assert result.usage.input_tokens == estimate_messages_tokens(messages)


def test_openai_usage_zero_counts_fall_back_to_estimate():
    """A present usage block reporting zeros still estimates (non-zero)."""
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    messages = [{"role": "user", "content": "a non-empty prompt"}]
    resp = _openai_resp(usage=usage, content="a non-empty reply")

    result = parse_openai_response(resp, messages)

    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert result.usage.estimated is True


def test_openai_usage_estimate_includes_tool_call_structure_and_tool_schema():
    """Usage fallbacks must charge serialized requests, not only prose content."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "r" * 3_000,
            "tool_calls": [{
                "id": "call_large_payload",
                "type": "function",
                "function": {"name": "search_records", "arguments": "x" * 12_000},
            }],
        },
        {"role": "tool", "tool_call_id": "call_large_payload", "content": ""},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "search_records",
            "description": "d" * 6_000,
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }]
    resp = _openai_resp(usage=None, content="ok")

    result = parse_openai_response(resp, messages, tools)

    assert result.usage.input_tokens == estimate_messages_tokens(messages, tools)
    assert result.usage.input_tokens > 6_000
    assert result.usage.input_tokens > estimate_messages_tokens(messages)


def test_openai_normal_usage_unchanged_and_no_double_count():
    """A normal usage block is used verbatim; cached_tokens is NOT added on top.

    OpenAI ``prompt_tokens`` already includes cached tokens, so the accounted
    input must equal prompt_tokens exactly (no additive cache fix here).
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=50,
        # cached_tokens lives under prompt_tokens_details and is already
        # counted inside prompt_tokens — must not be added again.
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    messages = [{"role": "user", "content": "irrelevant when usage present"}]
    resp = _openai_resp(usage=usage, content="reply")

    result = parse_openai_response(resp, messages)

    assert result.usage.input_tokens == 1000  # exactly prompt_tokens, no +800
    assert result.usage.output_tokens == 50
    assert result.usage.cache_read_tokens == 800
    assert result.usage.raw_usage["prompt_tokens_details"]["cached_tokens"] == 800
    assert result.usage.total_tokens == 1050
    assert result.usage.estimated is False


# ---------------------------------------------------------------------------
# Thinking passthrough — OpenAI-compatible extra_body (DashScope compatible mode)
# ---------------------------------------------------------------------------


def test_openai_thinking_on_adds_extra_body():
    """thinking=True ships thinking_params as extra_body for the SDK create()."""
    kwargs = build_openai_kwargs(
        "kimi-k2.6",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        thinking=True,
        thinking_params={"enable_thinking": True},
    )
    assert kwargs["extra_body"] == {"enable_thinking": True}


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "stream_options",
    ],
)
def test_openai_thinking_rejects_framework_controlled_request_fields(field):
    with pytest.raises(ValueError, match=field):
        build_openai_kwargs(
            "safe-model",
            [{"role": "user", "content": "keep this message"}],
            None,
            0.2,
            thinking=True,
            thinking_params={field: "override"},
            max_output_tokens=10,
        )


def test_openai_preserves_reasoning_content_for_tool_follow_up():
    kwargs = build_openai_kwargs(
        "kimi-for-coding",
        [
            {"role": "user", "content": "inspect the repository"},
            {
                "role": "assistant",
                "reasoning_content": "I need to read the target file.",
                "provider_state": {"anthropic_content": [{"type": "thinking"}]},
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
        ],
        None,
        0.2,
        thinking=True,
        thinking_params={"thinking": {"type": "enabled", "keep": "all"}},
    )

    assistant = kwargs["messages"][1]
    assert assistant["reasoning_content"] == "I need to read the target file."
    assert "provider_state" not in assistant
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled", "keep": "all"}}


def test_openai_thinking_off_adds_no_extra_body():
    """thinking=False leaves the request unchanged — no extra_body key."""
    kwargs = build_openai_kwargs(
        "kimi-k2.6",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        thinking=False,
        thinking_params={"enable_thinking": True},
    )
    assert "extra_body" not in kwargs


def test_openai_tool_choice_defaults_to_auto():
    """No tool_choice -> the request keeps the prior 'auto' default."""
    kwargs = build_openai_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        0.0,
    )
    assert kwargs["tool_choice"] == "auto"


def test_openai_tool_choice_required_is_passed_through():
    """tool_choice='required' overrides the 'auto' default for the forced-write path."""
    kwargs = build_openai_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        0.0,
        tool_choice="required",
    )
    assert kwargs["tool_choice"] == "required"


@pytest.mark.parametrize(
    ("thinking", "tool_choice"),
    [
        (True, "required"),
        (True, {"type": "function", "function": {"name": "structured_output"}}),
        (False, {"type": "function", "function": {"name": "structured_output"}}),
    ],
)
@pytest.mark.parametrize("model", ["k3", "kimi-for-coding"])
def test_openai_kimi_uses_auto_for_unsupported_forced_tool_choice(thinking, tool_choice, model):
    kwargs = build_openai_kwargs(
        model,
        [{"role": "user", "content": "write the final patch"}],
        [{"type": "function", "function": {"name": "write_file", "parameters": {}}}],
        1.0,
        thinking=thinking,
        thinking_params={"thinking": {"type": "enabled", "keep": "all"}},
        tool_choice=tool_choice,
    )

    assert kwargs["tool_choice"] == "auto"


def test_openai_other_thinking_model_keeps_required_tool_choice():
    kwargs = build_openai_kwargs(
        "another-thinking-model",
        [{"role": "user", "content": "write"}],
        [{"type": "function", "function": {"name": "write_file", "parameters": {}}}],
        1.0,
        thinking=True,
        thinking_params={"thinking": {"type": "enabled", "keep": "all"}},
        tool_choice="required",
    )

    assert kwargs["tool_choice"] == "required"


def test_openai_other_model_keeps_named_tool_choice():
    choice = {"type": "function", "function": {"name": "structured_output"}}
    kwargs = build_openai_kwargs(
        "another-thinking-model",
        [{"role": "user", "content": "write"}],
        [{"type": "function", "function": {"name": "structured_output", "parameters": {}}}],
        1.0,
        tool_choice=choice,
    )

    assert kwargs["tool_choice"] == choice


@pytest.mark.parametrize(
    ("model", "context_window"),
    [("k3", 1_048_576), ("kimi-for-coding", 262_144)],
)
def test_exact_model_capabilities_centralize_provider_compatibility(model, context_window):
    capabilities = model_capabilities(model)

    assert capabilities.context_window == context_window
    assert capabilities.supports_forced_tool_choice is False
    assert capabilities.honors_workflow_thinking_override is False


def test_unknown_model_capabilities_use_neutral_defaults():
    capabilities = model_capabilities("unknown-model")

    assert capabilities.context_window is None
    assert capabilities.supports_forced_tool_choice is True
    assert capabilities.honors_workflow_thinking_override is True


# ---------------------------------------------------------------------------
# top_p nucleus sampling — included only when set, omitted (request unchanged)
# when None. Mirrors the temperature/thinking conditional flow.
# ---------------------------------------------------------------------------


def test_openai_top_p_included_when_set():
    """top_p set -> the request carries the knob with that exact value."""
    kwargs = build_openai_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        top_p=0.9,
    )
    assert kwargs["top_p"] == 0.9


def test_openai_top_p_omitted_when_none():
    """top_p None (default) -> no top_p key, so the request is unchanged."""
    kwargs = build_openai_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
    )
    assert "top_p" not in kwargs


def test_anthropic_top_p_included_when_set():
    """Anthropic parity: top_p set -> the payload carries it."""
    kwargs = build_anthropic_kwargs(
        "claude",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        top_p=0.8,
    )
    assert kwargs["top_p"] == 0.8


def test_anthropic_top_p_omitted_when_none():
    """Anthropic parity: top_p None -> no top_p key (request unchanged)."""
    kwargs = build_anthropic_kwargs(
        "claude",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
    )
    assert "top_p" not in kwargs


def test_anthropic_keeps_compaction_record_in_conversation_order():
    identity = "global identity"
    compacted = "[Context auto-compacted]: earlier work"
    system_parts, messages = convert_to_anthropic_messages(
        [
            {"role": "system", "content": identity},
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "system", "content": compacted, "compacted": True},
            {"role": "user", "content": "new request"},
        ]
    )

    assert system_parts == [identity]
    assert [message["content"] for message in messages] == [
        "old request",
        [{"type": "text", "text": "old answer"}],
        compacted,
        "new request",
    ]


def test_max_output_tokens_reaches_both_provider_payloads():
    messages = [{"role": "user", "content": "hi"}]

    openai_kwargs = build_openai_kwargs(
        "glm-5.2", messages, None, 1.0, max_output_tokens=32768
    )
    anthropic_kwargs = build_anthropic_kwargs(
        "glm-5.2", messages, None, 1.0, max_output_tokens=32768
    )

    assert openai_kwargs["max_tokens"] == 32768
    assert anthropic_kwargs["max_tokens"] == 32768


def test_anthropic_tool_choice_required_maps_to_any():
    """Anthropic wants a dict form; 'required' -> {'type': 'any'}, None omits it."""
    tools = [{"function": {"name": "f", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hi"}]
    forced = build_anthropic_kwargs("claude", msgs, tools, 0.0, tool_choice="required")
    assert forced["tool_choice"] == {"type": "any"}
    default = build_anthropic_kwargs("claude", msgs, tools, 0.0)
    assert "tool_choice" not in default


def test_anthropic_tool_choice_none_maps_to_none_type():
    tools = [{"function": {"name": "f", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hi"}]

    kwargs = build_anthropic_kwargs("claude", msgs, tools, 0.0, tool_choice="none")

    assert kwargs["tool_choice"] == {"type": "none"}


def test_anthropic_tool_choice_named_function_maps_to_named_tool():
    tools = [{"function": {"name": "structured_output", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hi"}]

    kwargs = build_anthropic_kwargs(
        "claude",
        msgs,
        tools,
        0.0,
        tool_choice={"type": "function", "function": {"name": "structured_output"}},
    )

    assert kwargs["tool_choice"] == {"type": "tool", "name": "structured_output"}


def test_llm_client_forwards_anthropic_base_url(monkeypatch):
    captured = {}

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=FakeAsyncAnthropic),
    )

    from opencollab.adapters.llm.client import LLMClient

    client = LLMClient(
        provider="anthropic",
        model="claude",
        api_key="k",
        base_url="http://proxy.local",
        request_timeout=12.0,
    )

    assert client.base_url == "http://proxy.local"
    assert captured["base_url"] == "http://proxy.local"
    assert captured["api_key"] == "k"
    assert captured["timeout"] == 12.0


@pytest.mark.asyncio
async def test_llm_client_closes_provider_once(monkeypatch):
    from opencollab.adapters.llm import client as client_module

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    provider_client = FakeAsyncOpenAI()
    monkeypatch.setattr(
        client_module.openai,
        "AsyncOpenAI",
        lambda **_kwargs: provider_client,
    )
    client = client_module.LLMClient(
        provider="openai",
        model="gpt-test",
        api_key="test-key",
    )

    await client.close()
    await client.close()

    assert provider_client.close_calls == 1


def test_openai_estimates_output_from_tool_calls_when_no_content():
    """Output estimate falls back to tool-call args when content is empty."""
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="run", arguments='{"x": 1}'),
    )
    messages = [{"role": "user", "content": "do it"}]
    resp = _openai_resp(usage=None, content=None, tool_calls=[tool_call])

    result = parse_openai_response(resp, messages)

    assert result.usage.output_tokens > 0
    assert result.usage.estimated is True


# ---------------------------------------------------------------------------
# reasoning_content harvest — rescue a genuinely empty thinking turn
# ---------------------------------------------------------------------------


def _openai_resp_with_reasoning(content, reasoning_content, tool_calls=None):
    message = SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning_content
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


def test_openai_harvests_reasoning_content_when_content_empty():
    """An empty-content, no-tool-call turn falls back to reasoning_content."""
    resp = _openai_resp_with_reasoning(content=None, reasoning_content="the answer is 42")
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content == "the answer is 42"
    assert result.reasoning == "the answer is 42"  # also recorded for the trajectory


def test_openai_keeps_real_content_over_reasoning():
    """When content is present it wins — but reasoning is still recorded."""
    resp = _openai_resp_with_reasoning(content="real answer", reasoning_content="scratch work")
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content == "real answer"
    assert result.reasoning == "scratch work"  # kept for trajectory observability


def test_openai_does_not_harvest_reasoning_on_tool_call_turn():
    """A tool-call turn legitimately has empty content; don't inject reasoning."""
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="run", arguments="{}"))
    resp = _openai_resp_with_reasoning(content=None, reasoning_content="thinking", tool_calls=[tc])
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content is None
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "run"


# ---------------------------------------------------------------------------
# Anthropic thinking-block harvest — mirror of the OpenAI reasoning harvest
# ---------------------------------------------------------------------------


def _anthropic_resp(blocks, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    return SimpleNamespace(content=blocks, usage=usage, stop_reason=stop_reason)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _thinking_block(text, signature=None):
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def _tool_use_block(name="run", input=None):
    return SimpleNamespace(type="tool_use", id="c1", name=name, input=input or {})


def test_anthropic_harvests_thinking_when_no_text():
    """An empty-text, no-tool turn falls back to the thinking-block text."""
    resp = _anthropic_resp([_thinking_block("the answer is 42")])
    result = parse_anthropic_response(resp)
    assert result.content == "the answer is 42"
    assert result.reasoning == "the answer is 42"  # also recorded for the trajectory


def test_anthropic_keeps_real_text_over_thinking():
    """When a text block is present it wins — but thinking is still recorded."""
    resp = _anthropic_resp([_thinking_block("scratch"), _text_block("real answer")])
    result = parse_anthropic_response(resp)
    assert result.content == "real answer"
    assert result.reasoning == "scratch"  # kept for trajectory observability


def test_anthropic_does_not_harvest_thinking_on_tool_call_turn():
    """A tool-call turn legitimately has empty text; don't inject thinking."""
    resp = _anthropic_resp([_thinking_block("plan"), _tool_use_block(name="run")])
    result = parse_anthropic_response(resp)
    assert result.content is None
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "run"


def test_anthropic_preserves_signed_thinking_for_tool_follow_up():
    resp = _anthropic_resp([
        _thinking_block("plan", signature="signed-thinking"),
        _tool_use_block(name="run"),
    ])

    result = parse_anthropic_response(resp)
    message = {
        "role": "assistant",
        "tool_calls": result.tool_calls,
        "provider_state": result.provider_state,
    }
    _, converted = convert_to_anthropic_messages([message])

    assert converted[0]["content"][0] == {
        "type": "thinking",
        "thinking": "plan",
        "signature": "signed-thinking",
    }
    assert converted[0]["content"][1]["type"] == "tool_use"


def test_anthropic_conversion_normalizes_openai_text_blocks_without_mutation():
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "system "},
                {"type": "text", "text": "policy"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        },
    ]
    original = json.loads(json.dumps(messages))

    system, converted = convert_to_anthropic_messages(messages)

    assert system == ["system policy"]
    assert converted == [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ],
    }]
    assert messages == original


def test_anthropic_conversion_rejects_unsupported_content_block_with_location():
    messages = [
        {"role": "user", "content": "before"},
        {
            "role": "assistant",
            "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
        },
    ]

    with pytest.raises(
        ValueError,
        match=r"message 1 .*assistant.* unsupported content block type: image_url",
    ):
        convert_to_anthropic_messages(messages)


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ('{"path":', "invalid JSON"),
        ('"path"', "JSON object"),
        ("[]", "JSON object"),
        (b"{}", "text or an object"),
        (None, "text or an object"),
    ],
)
def test_anthropic_conversion_rejects_invalid_restored_tool_arguments(
    arguments,
    reason,
):
    messages = [
        {"role": "user", "content": "before"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-bad",
                "type": "function",
                "function": {"name": "file_read", "arguments": arguments},
            }],
        },
    ]

    with pytest.raises(
        ValueError,
        match=rf"message 1 .*file_read.*call-bad.*{reason}",
    ):
        convert_to_anthropic_messages(messages)


def test_anthropic_conversion_preserves_object_tool_arguments():
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call-ok",
            "type": "function",
            "function": {
                "name": "file_read",
                "arguments": '{"path": "src/a.py", "options": {"line": 2}}',
            },
        }],
    }]

    _, converted = convert_to_anthropic_messages(messages)

    assert converted[0]["content"][0]["input"] == {
        "path": "src/a.py",
        "options": {"line": 2},
    }


def test_anthropic_redacted_thinking_is_not_harvested():
    """redacted_thinking holds encrypted data, not text — must not become content."""
    redacted = SimpleNamespace(type="redacted_thinking", data="encrypted-bytes")
    resp = _anthropic_resp([redacted])
    result = parse_anthropic_response(resp)
    assert result.content is None
    assert result.provider_state == {
        "anthropic_content": [
            {"type": "redacted_thinking", "data": "encrypted-bytes"}
        ]
    }


# ---------------------------------------------------------------------------
# P6 — kimi literal tool-call markup recovery (finish_reason='stop', no
# parsed tool_calls; the call is embedded as special-token text in content)
# ---------------------------------------------------------------------------


def _markup(name, call_id, args_json):
    return (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>"
        f"functions.{name}:{call_id}"
        "<|tool_call_argument_begin|>"
        f"{args_json}"
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )


def test_openai_markup_single_tool_call_is_synthesized():
    """A single markup tool call -> one synthesized tool_call, content cleared."""
    content = _markup("grep", "call_1", '{"pattern": "foo"}')
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "grep"
    assert json.loads(tc["function"]["arguments"]) == {"pattern": "foo"}
    assert result.content is None
    assert result.usage.markup_recovered == 1  # P6 recovery counter fires


def test_openai_markup_two_tool_calls_are_synthesized():
    """Two markup blocks -> two synthesized tool_calls in order."""
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.grep:c1"
        '<|tool_call_argument_begin|>{"pattern": "a"}<|tool_call_end|>'
        "<|tool_call_begin|>functions.read_file:c2"
        '<|tool_call_argument_begin|>{"path": "x.py"}<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert [tc["function"]["name"] for tc in result.tool_calls] == ["grep", "read_file"]
    assert [tc["id"] for tc in result.tool_calls] == ["c1", "c2"]
    assert result.content is None


def test_openai_markup_malformed_group_is_not_partially_executed():
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.file_read:c1"
        '<|tool_call_argument_begin|>{"path": "safe.txt"}<|tool_call_end|>'
        "<|tool_call_begin|>functions.file_write:c2"
        '<|tool_call_argument_begin|>{"path": "target.txt"<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert result.tool_calls == []
    assert result.content == content
    assert result.usage.markup_recovered == 0


def test_openai_markup_preserves_surrounding_prose():
    """Genuine prose around the markup is preserved; the call is still synthesized."""
    content = "Let me search the repo. " + _markup("grep", "c1", '{"pattern": "foo"}')
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "grep"
    assert result.content == "Let me search the repo."


def test_openai_malformed_markup_falls_back_gracefully():
    """Partial markup / non-JSON args -> no crash, no synthesized call, content kept."""
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.grep:c1"
        "<|tool_call_argument_begin|>not-json-args"  # truncated, invalid JSON
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert result.tool_calls == []
    assert result.content == content  # unchanged fallback
    assert result.usage.markup_recovered == 0  # nothing recovered -> counter stays 0


def test_openai_real_tool_calls_skip_markup_parsing():
    """When the SDK already parsed tool_calls, markup recovery is not attempted."""
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="run", arguments="{}"))
    resp = _openai_resp(usage=None, content="irrelevant", tool_calls=[tc])

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "run"
    assert result.content == "irrelevant"
    assert result.usage.markup_recovered == 0  # structured calls -> no recovery


def test_openai_markup_in_reasoning_content_is_synthesized():
    """Under thinking mode kimi puts the tool-call markup in reasoning_content
    (content empty). Recover it from reasoning, not just content."""
    resp = _openai_resp_with_reasoning(
        content=None, reasoning_content=_markup("file_read", "2", '{"path": "a.py"}')
    )

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "file_read"
    assert result.tool_calls[0]["id"] == "2"
    # the markup must NOT leak into content via the reasoning rescue
    assert result.content is None
    assert result.reasoning is None  # whole reasoning was markup -> stripped
    assert result.usage.markup_recovered == 1  # recovered from reasoning_content


def test_openai_markup_buried_in_reasoning_preserves_thinking():
    """Real thinking prose followed by buried markup -> call synthesized, the
    genuine chain-of-thought is preserved in reasoning."""
    reasoning = "Let me check the test.\n\n" + _markup("grep", "1", '{"pattern": "X"}')
    resp = _openai_resp_with_reasoning(content=None, reasoning_content=reasoning)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "grep"
    assert result.content is None
    assert result.reasoning == "Let me check the test."


def test_openai_content_markup_takes_precedence_over_reasoning():
    """If markup is in content, parse there; don't double-parse reasoning."""
    resp = _openai_resp_with_reasoning(
        content=_markup("grep", "c1", '{"pattern": "a"}'),
        reasoning_content=_markup("read_file", "c2", '{"path": "x"}'),
    )

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert [tc["function"]["name"] for tc in result.tool_calls] == ["grep"]
    assert result.content is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, "-2"])
def test_openai_usage_rejects_non_finite_and_negative_values(value):
    assert openai_usage_int({"tokens": value}, "tokens") == 0
