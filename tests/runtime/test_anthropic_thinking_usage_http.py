"""Thinking usage retains SDK dict and object details in responses and ledgers."""

import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.anthropic_provider import _parse_usage
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import http_response, install_sdk_transport


@pytest.mark.parametrize("details,expected", [({"thinking_tokens": 312}, 312), ({"thinking_tokens": 0}, 0), ({}, None)])
async def test_anthropic_thinking_usage_from_sdk_reaches_ledger(monkeypatch, tmp_path, details, expected):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(ledger))

    async def handler(request):
        return http_response(request, 200, json={
            "id": "msg_usage", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 25, "output_tokens": 348, "output_tokens_details": details},
        })

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with LLMClient(
        model="claude-sonnet-4-6", provider="anthropic", api_key="test-placeholder",  # pragma: allowlist secret
        base_url="https://provider.invalid/v1", max_retries=0,
    ) as client:
        response = await client.complete(
            [{"role": "user", "content": "solve"}], thinking=True,
            thinking_params={"thinking": {"type": "adaptive", "display": "omitted"}},
        )

    assert response.usage.input_tokens == 25
    assert response.usage.output_tokens == 348
    assert response.usage.total_tokens == 373
    assert response.usage.reasoning_tokens == expected
    assert response.usage.raw_usage["output_tokens_details"] == details
    record = json.loads(ledger.read_text().splitlines()[-1])
    assert record["usage"]["reasoning_tokens"] == expected
    assert record["usage"]["raw_usage"]["output_tokens_details"] == details
    assert record["usage"]["total_tokens"] == 373


@pytest.mark.parametrize("shape", [dict, SimpleNamespace])
@pytest.mark.parametrize("thinking_tokens", [312, 0, None])
def test_anthropic_thinking_detail_shapes_keep_missing_and_zero_distinct(shape, thinking_tokens):
    details = shape(**({"thinking_tokens": thinking_tokens} if thinking_tokens is not None else {}))
    usage = SimpleNamespace(
        input_tokens=25, output_tokens=348, cache_read_input_tokens=2, cache_creation_input_tokens=3,
        output_tokens_details=details,
    )
    parsed = _parse_usage(usage)

    assert parsed.reasoning_tokens == thinking_tokens
    assert parsed.total_tokens == 378
    assert parsed.raw_usage["output_tokens_details"] == (
        {"thinking_tokens": thinking_tokens} if thinking_tokens is not None else {}
    )


def test_anthropic_absent_thinking_detail_remains_unreported():
    parsed = _parse_usage(SimpleNamespace(input_tokens=25, output_tokens=348))
    assert parsed.reasoning_tokens is None
    assert parsed.raw_usage["output_tokens_details"] is None
    assert parsed.total_tokens == 373
