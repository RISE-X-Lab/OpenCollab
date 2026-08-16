"""Focused request-shaping contracts for the OpenAI Responses adapter."""

from __future__ import annotations

import pytest

from opencollab.adapters.llm.responses_provider import (
    ResponsesProtocolError,
    _build_request_kwargs,
    _messages_to_input,
)
from opencollab.adapters.llm.types import model_capabilities


def test_responses_preserves_image_url_content_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is shown?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.test/a.png",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    _, items = _messages_to_input(messages)

    assert items == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is shown?"},
                {
                    "type": "input_image",
                    "image_url": "https://example.test/a.png",
                    "detail": "high",
                },
            ],
        }
    ]


def test_responses_restored_assistant_parts_remain_easy_input_content():
    _, items = _messages_to_input(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Recovered answer."},
                ],
            }
        ]
    )

    assert items == [
        {
            "role": "assistant",
            "content": [
                {"type": "input_text", "text": "Recovered answer."},
            ],
        }
    ]


def test_responses_restored_assistant_image_remains_easy_input_content():
    _, items = _messages_to_input(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/a.png"},
                    }
                ],
            }
        ]
    )

    assert items == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "https://example.test/a.png",
                }
            ],
        }
    ]


def test_responses_rejects_tools_for_models_without_function_calling():
    with pytest.raises(
        ResponsesProtocolError,
        match="does not support function tools",
    ):
        _build_request_kwargs(
            "o1-mini",
            [{"role": "user", "content": "work"}],
            [
                {
                    "type": "function",
                    "function": {"name": "f", "parameters": {}},
                }
            ],
            0.2,
        )


@pytest.mark.parametrize(
    "model",
    [
        "o1-pro",
        "vendor/o1-pro-2025-04-01",
        "o3-mini",
        "gateway/o3-mini-2026-01-01",
    ],
)
def test_responses_omits_default_sampling_for_models_without_sampling(model):
    kwargs = _build_request_kwargs(
        model,
        [{"role": "user", "content": "work"}],
        None,
        0.2,
    )
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert kwargs["stream"] is ("o1-pro" not in model)


@pytest.mark.parametrize(
    "model",
    ["o1-pro", "vendor/o1-pro-2025-04-01", "o3-mini"],
)
def test_responses_rejects_explicit_top_p_for_models_without_sampling(model):
    with pytest.raises(
        ResponsesProtocolError,
        match="does not support explicit top_p",
    ):
        _build_request_kwargs(
            model,
            [{"role": "user", "content": "work"}],
            None,
            0.2,
            top_p=0.9,
        )


@pytest.mark.parametrize(
    "model",
    ["gpt-4o", "vendor/gpt-4o-mini-2026-01-01"],
)
def test_responses_rejects_explicit_reasoning_for_non_reasoning_models(model):
    with pytest.raises(
        ResponsesProtocolError,
        match="does not support explicit reasoning_effort",
    ):
        _build_request_kwargs(
            model,
            [{"role": "user", "content": "work"}],
            None,
            0.2,
            reasoning_effort="low",
        )


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4.1",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
        "deepseek-v4-flash",
        "k3",
        "kimi-for-coding",
        "unknown-gateway-model",
    ],
)
def test_responses_reasoning_metadata_is_fail_closed(model):
    kwargs = _build_request_kwargs(
        model,
        [{"role": "user", "content": "work"}],
        None,
        0.2,
    )
    assert "include" not in kwargs

    with pytest.raises(ResponsesProtocolError, match="reasoning_effort"):
        _build_request_kwargs(
            model,
            [{"role": "user", "content": "work"}],
            None,
            0.2,
            reasoning_effort="low",
        )


@pytest.mark.parametrize("model", ["o1-preview", "gateway/o3-pro"])
def test_responses_reasoning_family_keeps_known_context_window(model):
    capabilities = model_capabilities(model)

    assert capabilities.supports_responses_reasoning is True
    assert capabilities.context_window == 200_000
