"""Public agents preserve native Claude sampling through the real SDK."""

from __future__ import annotations

import json

import anthropic
import pytest

from opencollab import OpenCollab
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport

_CLAUDE_4_MODELS = [
    "claude-sonnet-4", "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5",
    "claude-opus-4-6", "claude-sonnet-4-6", "claude-sonnet-4-20250514",
    "claude-opus-4-1-20250805", "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001", "gateway/claude-sonnet-4-6-20260217",
]

@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("OPENCOLLAB_TEMPERATURE", raising=False)
    monkeypatch.delenv("OPENCOLLAB_TOP_P", raising=False)

@pytest.mark.parametrize("model", _CLAUDE_4_MODELS)
@pytest.mark.parametrize("top_p", [0.8, 0.0])
async def test_public_agent_sends_only_top_p(monkeypatch, tmp_path, model, top_p):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "temperature" in body and "top_p" in body:
            return http_response(request, 400, json={
                "type": "error", "error": {"type": "invalid_request_error",
                    "message": "temperature and top_p cannot both be specified for Claude 4.x"},
            })
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with anthropic.AsyncAnthropic(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as sdk:
        response = await sdk.messages.create(
            model=model, messages=[{"role": "user", "content": "Return ok"}],
            max_tokens=2048, extra_body={"top_p": top_p},
        )
    assert response.content[-1].text == "ok"
    assert len(requests) == 1
    assert requests[0]["top_p"] == top_p
    assert "temperature" not in requests[0]

    result = await OpenCollab(
        tmp_path, provider="anthropic", model=model,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": False, "top_p": top_p, "max_output_tokens": 2048,
                "llm_max_retries": 0},
    ).agent("Return ok", tools=(), trace=False, budget=10000, max_steps=1)

    assert result.ok, result.error
    assert result.output == "ok"
    assert len(requests) == 2
    assert requests[1]["model"] == model
    assert requests[1]["max_tokens"] == 2048
    assert requests[1]["top_p"] == top_p
    assert "temperature" not in requests[1]
    assert "thinking" not in requests[1]

@pytest.mark.parametrize("model", [
    "claude-3-7-sonnet-20250219", "claude-3-5-haiku-20241022",
    "gateway/claude-3-5-sonnet-20241022", "compatible-unknown-model", "deepseek-flash",
])
async def test_public_agent_keeps_legacy_and_compatible_sampling(monkeypatch, tmp_path, model):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    result = await OpenCollab(
        tmp_path, provider="anthropic", model=model,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": False, "top_p": 0.8, "max_output_tokens": 2048,
                "llm_max_retries": 0},
    ).agent("Return ok", tools=(), trace=False, budget=10000, max_steps=1)

    assert result.ok, result.error
    assert result.output == "ok" and len(requests) == 1
    assert requests[0]["temperature"] == 0.2
    assert requests[0]["top_p"] == 0.8

@pytest.mark.parametrize("model,expected_temperature", [
    ("claude-sonnet-4", 0.2), ("claude-sonnet-4-6", 0.2),
    ("gateway/claude-sonnet-4-5-20250929", 0.2), ("claude-3-7-sonnet-20250219", 0.2),
    ("compatible-unknown-model", 0.2), ("claude-opus-4-7", None), ("claude-sonnet-5", None),
])
async def test_public_agent_keeps_sampling_defaults(monkeypatch, tmp_path, model, expected_temperature):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append((request.headers["anthropic-version"], body))
        if request.headers["anthropic-version"] == "2026-10-21" and "temperature" in body:
            return http_response(request, 400, json={
                "type": "error", "error": {"type": "invalid_request_error",
                    "message": "temperature is not supported in API version 2026-10-21"},
            })
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    result = await OpenCollab(
        tmp_path, provider="anthropic", model=model,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": False, "max_output_tokens": 2048, "llm_max_retries": 0},
    ).agent("Return ok", tools=(), trace=False, budget=10000, max_steps=1)

    assert result.ok, result.error
    assert result.output == "ok" and len(requests) == 1
    api_version, body = requests[0]
    assert api_version == "2023-06-01"
    assert "top_p" not in body
    assert ("temperature" in body) is (expected_temperature is not None)
    if expected_temperature is not None:
        assert body["temperature"] == expected_temperature


@pytest.mark.parametrize("credential", ["api_key", "auth_token"])
async def test_explicit_sdk_headers_keep_gateway_version_and_key(monkeypatch, tmp_path, credential):
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "anthropic-version: 2026-10-21\nX-Api-Key: fixture-gateway-key",  # pragma: allowlist secret
    )
    requests = []

    def handler(request):
        requests.append((
            request.headers["anthropic-version"],
            request.headers.get("x-api-key"),
            request.headers.get("authorization"),
        ))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    client = OpenCollab(
        tmp_path, provider="anthropic", model="claude-sonnet-5",
        base_url="https://controlled.invalid",
        config={credential: "fixture-sdk-credential", "llm_max_retries": 0},  # pragma: allowlist secret
    )
    async with client.create_model_client() as model:
        response = await model.complete([{"role": "user", "content": "Return ok"}])

    assert response.content == "ok"
    if credential == "api_key":
        assert requests == [("2026-10-21", "fixture-gateway-key", None)]
    else:
        assert requests == [("2026-10-21", None, "Bearer fixture-sdk-credential")]
