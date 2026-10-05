"""Reasoning aliases participate in output usage through the real OpenAI SDK."""

from __future__ import annotations

import httpx
import openai
import pytest

from opencollab.adapters.llm._chat_response import _parse_response
from opencollab.adapters.llm.client import LLMClient

_REQUEST = [{"role": "user", "content": "Compute 6 * 7."}]
_REASONING = "Intermediate reasoning text. " * 2_000
_USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}
_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _wire_response(message, usage):
    return {
        "id": "chatcmpl-reasoning-alias-usage",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        **({"usage": usage} if usage is not None else {}),
    }


async def _complete(content, reasoning_fields, usage):
    requests = []
    message = {"role": "assistant", "content": content, **reasoning_fields}

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_wire_response(message, usage))

    client = LLMClient(model="gpt-4o", api_key="unused", max_retries=0)  # pragma: allowlist secret
    await client._openai.close()
    client._openai = openai.AsyncOpenAI(
        api_key="unused",  # pragma: allowlist secret
        base_url="https://reasoning-alias-usage.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        response = await client.complete(_REQUEST)
        assert len(requests) == 1
        return response
    finally:
        await client.close()


@pytest.mark.parametrize("content", [None, "42"], ids=["reasoning-only", "with-content"])
@pytest.mark.parametrize("usage", [None, _ZERO_USAGE, _USAGE], ids=["missing", "zero", "reported"])
async def test_non_streaming_reasoning_alias_usage_matches_primary(content, usage):
    alias = await _complete(content, {"reasoning": _REASONING}, usage)
    primary = await _complete(content, {"reasoning_content": _REASONING}, usage)

    assert alias.content == primary.content == (content or _REASONING)
    assert alias.reasoning == primary.reasoning == _REASONING
    assert alias.usage == primary.usage
    assert alias.usage.raw_usage == (usage or {})
    if usage == _USAGE:
        assert alias.usage.input_tokens == 13
        assert alias.usage.output_tokens == 7
        assert alias.usage.estimated is False
    else:
        assert alias.usage.output_tokens > 19_000
        assert alias.usage.estimated is True


@pytest.mark.parametrize("primary", ["preferred", "", 17], ids=["valid", "empty", "malformed"])
@pytest.mark.parametrize("usage", [None, _ZERO_USAGE], ids=["missing", "zero"])
async def test_reasoning_usage_estimates_only_the_selected_valid_field(primary, usage):
    selected = primary if primary == "preferred" else _REASONING
    response = await _complete(
        "42", {"reasoning_content": primary, "reasoning": _REASONING}, usage
    )
    reference = await _complete("42", {"reasoning_content": selected}, usage)

    assert response.reasoning == selected
    assert response.usage == reference.usage


def test_reasoning_usage_projection_preserves_the_sdk_message():
    message = {"role": "assistant", "content": None, "reasoning_content": 17, "reasoning": _REASONING}
    response = openai.types.chat.ChatCompletion.model_validate(_wire_response(message, None))
    original = response.choices[0].message.model_dump()

    parsed = _parse_response(response, _REQUEST)

    assert response.choices[0].message.model_dump() == original
    assert parsed.reasoning == _REASONING
    assert parsed.usage.output_tokens > 19_000
