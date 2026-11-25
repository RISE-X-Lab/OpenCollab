"""Retry timing from provider headers through the real SDK transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import openai
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


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(("status", "error_text", "retryable"), [
    (524, "A Timeout Occurred", True),
    (520, "Upstream temporarily unavailable", True),
    (501, "The timeout option is not implemented for this endpoint", False),
    (400, "Invalid timeout parameter", False),
])
async def test_gateway_status_and_error_text_control_retry(
    monkeypatch, tmp_path, provider, status, error_text, retryable,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    requests, delays = [], []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return http_response(
                request, status, headers={"Content-Type": "text/html", "Retry-After": "0.125"},
                text=f"<title>{status} {error_text}</title>",
            )
        return completion_http_response(request)

    async def record_delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(retry, "asyncio", SimpleNamespace(sleep=record_delay))
    monkeypatch.setattr(retry, "RETRY_JITTER_MAX_SECONDS", 0)
    install_sdk_transport(monkeypatch, provider, handle)
    async with LLMClient(
        provider=provider, model="claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o",
        api_key=tmp_path.name, base_url="https://controlled.invalid",
        max_retries=1,
    ) as client:
        messages = [{"role": "user", "content": "Reply ok."}]
        if retryable:
            result = await client.complete(messages)
            assert result.content == "ok"
            assert result.usage.total_tokens == 4
        else:
            with pytest.raises((openai.APIStatusError, anthropic.APIStatusError)) as captured:
                await client.complete(messages)
            assert captured.value.status_code == status

    assert len(requests) == (2 if retryable else 1)
    assert delays == ([0.125] if retryable else [])
