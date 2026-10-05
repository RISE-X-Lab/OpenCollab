"""Non-streaming reasoning aliases through the real OpenAI SDK transport."""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from opencollab.adapters.llm.client import LLMClient

_USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


async def _complete(message):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-reasoning-alias",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o",
            "usage": _USAGE,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        })

    client = LLMClient(model="gpt-4o", api_key="unused", max_retries=0)  # pragma: allowlist secret
    await client._openai.close()
    client._openai = openai.AsyncOpenAI(
        api_key="unused",  # pragma: allowlist secret
        base_url="https://reasoning-alias.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        response = await client.complete([{"role": "user", "content": "Compute 6 * 7."}])
        return response, requests
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("content", "reasoning_fields", "expected_content", "expected_reasoning"),
    [
        ("42", {"reasoning": "checked the multiplication"}, "42", "checked the multiplication"),
        (None, {"reasoning": "the answer is 42"}, "the answer is 42", "the answer is 42"),
        (
            "42",
            {"reasoning_content": "preferred", "reasoning": "secondary"},
            "42",
            "preferred",
        ),
        (
            "42",
            {"reasoning_content": "", "reasoning": "fallback after empty primary"},
            "42",
            "fallback after empty primary",
        ),
        (
            "42",
            {"reasoning_content": 17, "reasoning": "fallback after malformed primary"},
            "42",
            "fallback after malformed primary",
        ),
    ],
    ids=[
        "alias-with-content",
        "alias-only-rescue",
        "primary-precedence",
        "empty-primary",
        "malformed-primary",
    ],
)
async def test_non_streaming_reasoning_alias_matches_stream_contract(
    content, reasoning_fields, expected_content, expected_reasoning
):
    message = {"role": "assistant", "content": content, **reasoning_fields}

    response, requests = await _complete(message)

    assert response.content == expected_content
    assert response.reasoning == expected_reasoning
    assert response.usage.total_tokens == 20
    assert len(requests) == 1
