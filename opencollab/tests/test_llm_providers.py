"""Unit tests for shared provider classification + near-miss warning."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.anthropic_provider import _parse_usage as parse_anthropic_usage
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
