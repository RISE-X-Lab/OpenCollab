"""Anthropic thinking request contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from session_run_loop_test_support import build_runner

from opencollab.adapters.llm.anthropic_provider import (
    _build_request_kwargs as build_anthropic_kwargs,
)
from opencollab.adapters.llm.anthropic_provider import (
    _parse_response as parse_anthropic_response,
)
from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.domain.session import SessionState


def test_manual_thinking_uses_configured_budget():
    params = {"thinking": {"type": "enabled", "budget_tokens": 16_000}}

    kwargs = build_anthropic_kwargs(
        "claude-sonnet-4-5",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        thinking=True,
        thinking_params=params,
        max_output_tokens=32_768,
    )

    assert kwargs["thinking"] == params["thinking"]
    assert kwargs["max_tokens"] == 32_768
    assert "temperature" not in kwargs


def test_adaptive_thinking_forwards_effort():
    params = {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {
            "effort": "high",
            "format": {
                "type": "json_schema",
                "schema": {"type": "object", "properties": {}},
            },
        },
    }

    kwargs = build_anthropic_kwargs(
        "claude-sonnet-4-7",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        thinking=True,
        thinking_params=params,
        max_output_tokens=32_768,
    )

    assert kwargs["thinking"] == params["thinking"]
    assert kwargs["output_config"] == params["output_config"]
    assert "temperature" not in kwargs


def test_manual_thinking_uses_auto_for_forced_tool_choice():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-4-5",
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "run", "parameters": {}}}],
        0.2,
        thinking=True,
        thinking_params={
            "thinking": {"type": "enabled", "budget_tokens": 4096}
        },
        tool_choice="required",
        max_output_tokens=8192,
    )

    assert kwargs["tool_choice"] == {"type": "auto"}


def test_adaptive_thinking_keeps_forced_tool_choice():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-4-7",
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "run", "parameters": {}}}],
        0.2,
        thinking=True,
        thinking_params={"thinking": {"type": "adaptive"}},
        tool_choice="required",
    )

    assert kwargs["tool_choice"] == {"type": "any"}


@pytest.mark.parametrize(
    ("params", "error"),
    [
        (None, "provider-native"),
        ({"enable_thinking": True}, "unsupported"),
        ({"thinking": {"type": "enabled", "budget_tokens": 1000}}, "at least 1024"),
        (
            {"thinking": {"type": "enabled", "budget_tokens": 8192}},
            "less than max_output_tokens",
        ),
        (
            {"thinking": {"type": "adaptive", "budget_tokens": 4096}},
            "unsupported Anthropic thinking field",
        ),
        (
            {"thinking": {"type": "adaptive", "unexpected": True}},
            "unsupported Anthropic thinking field",
        ),
        (
            {"thinking": {"type": "adaptive", "display": "full"}},
            "display must be summarized or omitted",
        ),
        (
            {"thinking": {"type": "adaptive"}, "output_config": "high"},
            "output_config must be an object",
        ),
        (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "extreme"},
            },
            "unsupported Anthropic output_config.effort",
        ),
        (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"unexpected": True},
            },
            "unsupported Anthropic output_config field",
        ),
        (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"format": {"type": "json_schema"}},
            },
            "requires type and schema",
        ),
        (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {
                    "format": {"type": "json_schema", "schema": "not-an-object"}
                },
            },
            "must contain a JSON schema",
        ),
    ],
)
def test_thinking_rejects_invalid_or_ignored_parameters(params, error):
    with pytest.raises(ValueError, match=error):
        build_anthropic_kwargs(
            "model",
            [{"role": "user", "content": "hi"}],
            None,
            0.2,
            thinking=True,
            thinking_params=params,
            max_output_tokens=8192,
        )


def test_thinking_off_keeps_sampling_and_ignores_thinking_params():
    kwargs = build_anthropic_kwargs(
        "model",
        [{"role": "user", "content": "hi"}],
        None,
        0.2,
        thinking=False,
        thinking_params={"enable_thinking": True},
    )

    assert kwargs["temperature"] == 0.2
    assert "thinking" not in kwargs


def test_thinking_rejects_explicit_top_p():
    with pytest.raises(ValueError, match="provider-default top_p"):
        build_anthropic_kwargs(
            "model",
            [{"role": "user", "content": "hi"}],
            None,
            0.2,
            thinking=True,
            thinking_params={"thinking": {"type": "adaptive"}},
            top_p=0.95,
        )


def test_tool_round_preserves_all_anthropic_content_in_order():
    blocks = [
        SimpleNamespace(type="thinking", thinking="plan", signature="signed"),
        SimpleNamespace(type="redacted_thinking", data="encrypted"),
        SimpleNamespace(type="text", text="Checking now."),
        SimpleNamespace(type="tool_use", id="call-1", name="run", input={"cmd": "test"}),
    ]
    response = parse_anthropic_response(
        SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="tool_use",
        )
    )
    state = SessionState(messages=[{"role": "user", "content": "inspect"}])
    build_runner(state=state).append_assistant_message(response)
    state.messages.append(
        {"role": "tool", "tool_call_id": "call-1", "content": "passed"}
    )

    _, messages = convert_to_anthropic_messages(state.messages)

    assert messages[1]["content"] == [
        {"type": "thinking", "thinking": "plan", "signature": "signed"},
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "text", "text": "Checking now."},
        {"type": "tool_use", "id": "call-1", "name": "run", "input": {"cmd": "test"}},
    ]
    assert messages[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "passed"}
        ],
    }
