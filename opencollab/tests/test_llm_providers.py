"""Unit tests for shared provider classification + near-miss warning."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.anthropic_provider import _build_request_kwargs as build_anthropic_kwargs
from opencollab.adapters.llm.anthropic_provider import _parse_response as parse_anthropic_response
from opencollab.adapters.llm.anthropic_provider import _parse_usage as parse_anthropic_usage
from opencollab.adapters.llm.openai_provider import _build_request_kwargs as build_openai_kwargs
from opencollab.adapters.llm.openai_provider import _parse_response as parse_openai_response
from opencollab.adapters.llm.providers import (
    is_anthropic,
    normalize_provider,
    required_env_key,
    warn_provider_near_miss,
)
from opencollab.adapters.llm.types import estimate_messages_tokens


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


def test_anthropic_tool_choice_required_maps_to_any():
    """Anthropic wants a dict form; 'required' -> {'type': 'any'}, None omits it."""
    tools = [{"function": {"name": "f", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hi"}]
    forced = build_anthropic_kwargs("claude", msgs, tools, 0.0, tool_choice="required")
    assert forced["tool_choice"] == {"type": "any"}
    default = build_anthropic_kwargs("claude", msgs, tools, 0.0)
    assert "tool_choice" not in default


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


def _thinking_block(text):
    return SimpleNamespace(type="thinking", thinking=text)


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


def test_anthropic_redacted_thinking_is_not_harvested():
    """redacted_thinking holds encrypted data, not text — must not become content."""
    redacted = SimpleNamespace(type="redacted_thinking", data="encrypted-bytes")
    resp = _anthropic_resp([redacted])
    result = parse_anthropic_response(resp)
    assert result.content is None
