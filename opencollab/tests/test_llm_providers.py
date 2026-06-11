"""Unit tests for shared provider classification + near-miss warning."""

from __future__ import annotations

import logging

import pytest

from opencollab.adapters.llm.providers import (
    is_anthropic,
    normalize_provider,
    required_env_key,
    warn_provider_near_miss,
)


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
