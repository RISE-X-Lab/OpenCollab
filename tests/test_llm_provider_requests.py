"""Unit tests for provider request building — thinking, tool choice, sampling knobs."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from llm_provider_test_support import _openai_resp

from opencollab.adapters.llm.anthropic_provider import _build_request_kwargs as build_anthropic_kwargs
from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.adapters.llm.openai_provider import _build_request_kwargs as build_openai_kwargs
from opencollab.adapters.llm.openai_provider import _parse_response as parse_openai_response
from opencollab.adapters.llm.types import model_capabilities

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


@pytest.mark.parametrize(
    "model",
    [
        "vendor/kimi-for-coding",
        "gateway/kimi-for-coding-2026-08-07",
    ],
)
def test_known_model_capabilities_accept_bounded_provider_aliases(model):
    capabilities = model_capabilities(model)

    assert capabilities.context_window == 262_144
    assert capabilities.supports_forced_tool_choice is False
    assert capabilities.honors_workflow_thinking_override is False


@pytest.mark.parametrize(
    "model",
    [
        "vendor/kimi-for-coding-preview",
        "vendor/not-kimi-for-coding",
    ],
)
def test_known_model_capabilities_reject_prefixed_near_misses(model):
    capabilities = model_capabilities(model)

    assert capabilities.context_window is None
    assert capabilities.supports_forced_tool_choice is True


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


@pytest.mark.parametrize(
    "model",
    ["o1-preview", "o3-mini", "gateway/o1-2026-08-07"],
)
def test_openai_reasoning_models_use_compatible_request_fields(model):
    kwargs = build_openai_kwargs(
        model,
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        top_p=0.9,
        max_output_tokens=123,
    )

    assert kwargs["max_completion_tokens"] == 123
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


def test_openai_reasoning_model_near_miss_keeps_classic_fields():
    kwargs = build_openai_kwargs(
        "vendor/not-o1-preview",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        top_p=0.9,
        max_output_tokens=123,
    )

    assert kwargs["max_tokens"] == 123
    assert kwargs["temperature"] == 0.2
    assert kwargs["top_p"] == 0.9


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
    assert captured["max_retries"] == 0


def test_llm_client_disables_openai_sdk_retries(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    from opencollab.adapters.llm import client as client_module

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", FakeAsyncOpenAI)

    client = client_module.LLMClient(
        provider="openai",
        model="gpt-4o",
        api_key="k",
        max_retries=3,
    )

    assert client.max_retries == 3
    assert captured["max_retries"] == 0


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


def test_openai_usage_fallback_tolerates_null_tool_arguments():
    """A gateway's partial tool call must not crash usage estimation."""
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="structured_output", arguments=None),
    )
    messages = [{"role": "user", "content": "commit the structured result"}]
    resp = _openai_resp(usage=None, content=None, tool_calls=[tool_call])

    result = parse_openai_response(resp, messages)

    assert result.usage.output_tokens > 0
    assert result.usage.estimated is True
    assert result.tool_calls[0]["function"]["arguments"] == ""
