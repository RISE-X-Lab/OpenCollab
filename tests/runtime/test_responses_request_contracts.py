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


@pytest.mark.parametrize("part_type", ["text", "input_text", "output_text"])
def test_responses_normalizes_restored_text_block_variants(part_type):
    _, items = _messages_to_input(
        [
            {
                "role": "assistant",
                "content": [{"type": part_type, "text": "Recovered answer."}],
            }
        ]
    )

    assert items == [
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "Recovered answer."}],
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


def test_responses_preserves_assistant_phase_in_legacy_history():
    _, items = _messages_to_input(
        [
            {
                "role": "assistant",
                "phase": "commentary",
                "content": "Inspecting the workspace.",
            }
        ]
    )

    assert items == [
        {
            "role": "assistant",
            "phase": "commentary",
            "content": "Inspecting the workspace.",
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


def test_responses_known_deepseek_reasoning_is_explicitly_opted_in():
    kwargs = _build_request_kwargs(
        "deepseek-v4-flash-0731",
        [{"role": "user", "content": "work"}],
        None,
        0.2,
        reasoning_effort="max",
    )

    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["reasoning"] == {"effort": "max"}


def test_responses_known_luna_reasoning_is_explicitly_opted_in():
    kwargs = _build_request_kwargs(
        "gpt-5.6-luna",
        [{"role": "user", "content": "work"}],
        None,
        1.0,
        reasoning_effort="max",
    )

    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["reasoning"] == {"effort": "max"}


@pytest.mark.parametrize("model", ["o1-preview", "gateway/o3-pro"])
def test_responses_reasoning_family_keeps_known_context_window(model):
    capabilities = model_capabilities(model)

    assert capabilities.supports_responses_reasoning is True
    assert capabilities.context_window == 200_000


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-2025-08-07"])
@pytest.mark.parametrize("effort", [None, "none", "high"])
def test_native_gpt5_omits_unsupported_temperature(model, effort):
    kwargs = _build_request_kwargs(
        model, [{"role": "user", "content": "work"}], None, 0.2, reasoning_effort=effort,
    )

    assert "temperature" not in kwargs


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-luna-2026-10-14"])
@pytest.mark.parametrize("effort", [None, "none", "medium", "high"])
def test_native_gpt6_luna_omits_unsupported_temperature(model, effort):
    # The native endpoint rejects ``temperature`` for this model at every
    # effort level, so the exact capability entry turns sampling off.
    kwargs = _build_request_kwargs(
        model, [{"role": "user", "content": "work"}], None, 0.2, reasoning_effort=effort,
    )

    assert "temperature" not in kwargs


@pytest.mark.parametrize("model", [
    "gpt-5.1", "gpt-5.2", "gpt-5.2-2025-12-11", "gpt-5.4", "gpt-5.4-2026-03-05",
])
@pytest.mark.parametrize("effort", [None, "none", "low", "high", "xhigh"])
def test_native_gpt5_sampling_follows_effective_reasoning_effort(model, effort):
    kwargs = _build_request_kwargs(
        model, [{"role": "user", "content": "work"}], None, 0.2, reasoning_effort=effort,
    )

    if effort in (None, "none"):
        assert kwargs["temperature"] == 0.2
    else:
        assert "temperature" not in kwargs


@pytest.mark.parametrize("model,effort", [
    ("gpt-5", None), ("gpt-5-mini", "high"), ("gpt-5.2", "high"),
    ("gpt-5.4", "high"), ("gpt-5.4-2026-03-05", "high"),
])
def test_native_gpt5_rejects_explicit_unsupported_top_p(model, effort):
    with pytest.raises(ResponsesProtocolError, match="does not support explicit top_p"):
        _build_request_kwargs(
            model, [{"role": "user", "content": "work"}], None, 0.2,
            top_p=0.9, reasoning_effort=effort,
        )


@pytest.mark.parametrize("model", ["gpt-5.1", "gpt-5.2", "gpt-5.4"])
def test_native_gpt5_none_reasoning_accepts_explicit_top_p(model):
    kwargs = _build_request_kwargs(
        model, [{"role": "user", "content": "work"}], None, 0.2,
        top_p=0.9, reasoning_effort="none",
    )

    assert kwargs["top_p"] == 0.9


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.2", "gpt-5.4", "vendor/gpt-5-mini"])
def test_compatible_responses_provider_retains_its_sampling_parameters(model):
    kwargs = _build_request_kwargs(
        model, [{"role": "user", "content": "work"}], None, 0.2,
        top_p=0.9, reasoning_effort="high", native_openai=False,
    )

    assert kwargs["temperature"] == 0.2
    assert kwargs["top_p"] == 0.9


@pytest.mark.parametrize("provider,base_url,environment_url,native", [
    ("openai", None, None, True),
    (" OpenAI ", None, None, True),
    ("openai", "https://api.openai.com/v1", None, True),
    ("openai", "https://us.api.openai.com/v1", None, True),
    ("openai", "https://eu.api.openai.com/v1", None, True),
    ("openai", "https://au.api.openai.com/v1", None, True),
    ("openai", "https://ca.api.openai.com/v1", None, True),
    ("openai", "https://jp.api.openai.com/v1", None, True),
    ("openai", "https://in.api.openai.com/v1", None, True),
    ("openai", "https://sg.api.openai.com/v1", None, True),
    ("openai", "https://kr.api.openai.com/v1", None, True),
    ("openai", "https://gb.api.openai.com/v1", None, True),
    ("openai", "https://ae.api.openai.com/v1", None, True),
    ("openai", "https://gateway.example.invalid/v1", None, False),
    ("openai", "https://custom.api.openai.com/v1", None, False),
    ("openai", "https://eu.api.openai.com.example.invalid/v1", None, False),
    ("openai", None, "https://gateway.example.invalid/v1", False),
    ("openai", None, "https://eu.api.openai.com/v1", True),
    ("openai", None, "https://api.openai.com/v1", True),
    ("openai", "https://api.openai.com/v1", "https://gateway.example.invalid/v1", True),
    ("custom", None, None, False),
])
async def test_responses_client_applies_native_rules_only_to_native_endpoint(
    monkeypatch, provider, base_url, environment_url, native,
):
    from opencollab.adapters.llm import client as client_module
    from tests.support.responses_provider_test_support import completed_response, message_item

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    if environment_url is not None:
        monkeypatch.setenv("OPENAI_BASE_URL", environment_url)
    client = client_module.LLMClient(
        model="gpt-5", provider=provider, base_url=base_url, wire_protocol="responses",
        api_key="fixture-key",  # pragma: allowlist secret
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return completed_response(model="gpt-5", output=[message_item("done")])

    actual_complete = client_module.complete_responses

    async def complete_without_stream(*args, **kwargs):
        return await actual_complete(*args, **kwargs, stream=False)

    monkeypatch.setattr(client._openai.responses, "create", create)
    monkeypatch.setattr(client_module, "complete_responses", complete_without_stream)
    try:
        response = await client.complete([{"role": "user", "content": "work"}], temperature=0.2)
    finally:
        await client.close()

    assert response.content == "done"
    assert ("temperature" in requests[0]) is (not native)


@pytest.mark.parametrize("base_url", ["https://us.api.openai.com/v1", "https://eu.api.openai.com/v1"])
async def test_regional_responses_client_rejects_unsupported_top_p_before_request(monkeypatch, base_url):
    from opencollab.adapters.llm.client import LLMClient

    client = LLMClient(
        model="gpt-5.2", provider="openai", base_url=base_url, wire_protocol="responses",
        api_key="fixture-key",  # pragma: allowlist secret
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        raise AssertionError("unsupported parameters reached the provider")

    monkeypatch.setattr(client._openai.responses, "create", create)
    try:
        with pytest.raises(ResponsesProtocolError, match="does not support explicit top_p"):
            await client.complete(
                [{"role": "user", "content": "work"}], temperature=0.2,
                top_p=0.9, reasoning_effort="high",
            )
    finally:
        await client.close()

    assert requests == []
