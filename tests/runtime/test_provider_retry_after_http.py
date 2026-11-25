"""Retry timing from provider headers through the real SDK transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm import retry
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("headers", [
    {"Retry-After": "0.125"},
    {"Retry-After-Ms": "125"},
    {"Retry-After-Ms": "125", "Retry-After": "3"},
    {"Retry-After-Ms": "NaN", "Retry-After": "0.125"},
], ids=["seconds", "milliseconds", "milliseconds-preferred", "invalid-milliseconds-uses-seconds"])
async def test_provider_retry_delay_fits_budget(monkeypatch, tmp_path, protocol, headers):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(log_path))
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    monkeypatch.delenv("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO", raising=False)
    requests, delays = [], []

    def handle(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return http_response(request, 429, headers=headers, json={
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "Request rate limited"},
            })
        return completion_http_response(request, output_tokens=7)

    async def record_delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(retry, "asyncio", SimpleNamespace(sleep=record_delay))
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(retry, "RETRY_JITTER_MAX_SECONDS", 0)
    provider = "anthropic" if protocol == "anthropic" else "openai"
    install_sdk_transport(monkeypatch, provider, handle)
    async with LLMClient(
        provider=provider, model="claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o",
        wire_protocol="responses" if protocol == "responses" else "chat_completions",
        api_key="controlled-test", base_url="https://controlled.invalid/v1",  # pragma: allowlist secret
        max_retries=1, provider_error_time_budget=0.75,
    ) as client:
        response = await client.complete([{"role": "user", "content": "Reply ok."}])

    assert len(requests) == 2
    assert delays == [0.125]
    assert response.content == "ok"
    assert response.usage.total_tokens == 9
    record, = map(json.loads, log_path.read_text().splitlines())
    assert record["status"] == "success"
    assert record["transport_timing"]["attempts"] == 2
    assert record["usage"]["total_tokens"] == 9
