"""Exact model metadata used by request building and history compaction."""

from __future__ import annotations

import dataclasses

import pytest

from opencollab.adapters.llm.openai_provider import _build_request_kwargs
from opencollab.adapters.llm.types import ModelCapabilities, model_capabilities
from opencollab.application.shaping.pipeline import history_trigger_target


@pytest.mark.parametrize(
    ("model", "window", "thresholds"),
    [
        ("qwen3.8-flash", 983_616, (950_616, 712_962)),
        ("deepseek-v4.1-flash", 1_000_000, (967_000, 725_250)),
    ],
)
def test_exact_input_windows_drive_compaction(model, window, thresholds):
    capabilities = model_capabilities(model)

    assert capabilities.context_window == window
    assert history_trigger_target(capabilities.context_window) == thresholds


@pytest.mark.parametrize("prefix", ["", "gateway/"])
@pytest.mark.parametrize("suffix", ["", "-2026-09-21"])
@pytest.mark.parametrize("model", ["qwen3.8-flash", "deepseek-v4.1-flash"])
def test_exact_input_windows_accept_provider_prefixes_and_dates(prefix, suffix, model):
    assert model_capabilities(prefix + model + suffix) == model_capabilities(model)


@pytest.mark.parametrize(
    "choice",
    ["required", {"type": "function", "function": {"name": "edit"}}],
)
def test_qwen_thinking_request_uses_supported_tool_choice(choice):
    kwargs = _build_request_kwargs(
        "qwen3.8-flash",
        [{"role": "user", "content": "edit the file"}],
        [{"type": "function", "function": {"name": "edit", "parameters": {"type": "object"}}}],
        0.2,
        tool_choice=choice,
        reasoning_effort="max",
    )

    assert kwargs["tool_choice"] == "auto"
    assert kwargs["reasoning_effort"] == "max"


def test_deepseek_v41_retains_unmeasured_capability_defaults():
    assert dataclasses.replace(
        model_capabilities("deepseek-v4.1-flash"), context_window=None
    ) == ModelCapabilities()


def test_other_models_retain_their_own_windows_and_thinking_policy():
    luna = model_capabilities("gpt-5.6-luna")
    flash = model_capabilities("deepseek-v4-flash")

    assert luna.context_window == 1_048_576
    assert luna.supports_responses_reasoning is True
    assert luna.honors_workflow_thinking_override is False
    assert flash.context_window == 1_048_576
    assert flash.supports_forced_tool_choice is False
    assert flash.honors_workflow_thinking_override is False
    assert model_capabilities("deepseek-v4-pro").context_window == 64_000
    assert model_capabilities("qwen-unmeasured").context_window == 131_072

