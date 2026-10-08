"""Provider SDK HTTP fixtures preserve native retries and stream lifetimes."""

from __future__ import annotations

import json

import anthropic
import openai
import pytest

from tests.support.provider_sdk_http import (
    completion_http_response,
    http_response,
    install_sdk_transport,
    provider_http,
)


def _client(provider, **kwargs):
    sdk_type = anthropic.AsyncAnthropic if provider == "anthropic" else openai.AsyncOpenAI
    return sdk_type(
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", **kwargs,
    )


async def _create(sdk, provider, *, stream=False):
    kwargs = {"model": "test-model", "messages": [{"role": "user", "content": "Return ok"}], "stream": stream}
    if provider == "anthropic":
        return await sdk.messages.create(**kwargs, max_tokens=16)
    return await sdk.chat.completions.create(**kwargs)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_sdk_transport_retries_native_connection_timeout(monkeypatch, provider):
    http = provider_http(provider)
    requests = []

    def handler(request):
        assert isinstance(request, http.Request)
        requests.append(request)
        if len(requests) == 1:
            raise http.ConnectTimeout("Controlled connection timeout", request=request)
        response = completion_http_response(request)
        assert isinstance(response, http.Response)
        return response

    install_sdk_transport(monkeypatch, provider, handler)
    async with _client(provider, max_retries=1) as sdk:
        result = await _create(sdk, provider)
        assert (result.content[0].text if provider == "anthropic" else result.choices[0].message.content) == "ok"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert sdk.is_closed()


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("timeout_type", ["ConnectTimeout", "ReadTimeout"])
async def test_sdk_transport_preserves_native_timeout_causes(monkeypatch, provider, timeout_type):
    http = provider_http(provider)
    package = anthropic if provider == "anthropic" else openai
    requests = []
    failures = []

    def handler(request):
        requests.append(request)
        failure = getattr(http, timeout_type)("Controlled timeout", request=request)
        failures.append(failure)
        raise failure

    install_sdk_transport(monkeypatch, provider, handler)
    async with _client(provider, max_retries=0) as sdk:
        with pytest.raises(package.APITimeoutError) as captured:
            await _create(sdk, provider)
    assert len(requests) == 1
    assert captured.value.__cause__ is failures[0]
    assert captured.value.request is requests[0]
    assert sdk.is_closed()


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("fail", [False, True], ids=["two-events", "read-error"])
async def test_sdk_transport_reads_stream_lazily_and_preserves_errors(monkeypatch, provider, fail):
    http = provider_http(provider)
    failure = http.ReadError("Controlled stream failure")
    reads = []
    closed = []

    class EventStream(http.AsyncByteStream):
        async def __aiter__(self):
            for text in ("first", "second"):
                reads.append(text)
                if text == "second" and fail:
                    raise failure
                if provider == "anthropic":
                    event = {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": text}}
                    yield f"event: content_block_delta\ndata: {json.dumps(event)}\n\n".encode()
                else:
                    event = {"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
                             "model": "test-model", "choices": [
                                 {"index": 0, "delta": {"content": text}, "finish_reason": None},
                             ]}
                    yield f"data: {json.dumps(event)}\n\n".encode()

        async def aclose(self):
            closed.append(True)

    def handler(request):
        assert isinstance(request, http.Request)
        assert json.loads(request.content)["stream"] is True
        return http_response(request, 200, headers={"content-type": "text/event-stream"}, stream=EventStream())

    install_sdk_transport(monkeypatch, provider, handler)
    async with _client(provider, max_retries=0) as sdk:
        stream = await _create(sdk, provider, stream=True)
        assert reads == []
        assert closed == []
        iterator = stream.__aiter__()
        first = await anext(iterator)
        assert (first.delta.text if provider == "anthropic" else first.choices[0].delta.content) == "first"
        assert reads == ["first"]
        assert closed == []
        if fail:
            with pytest.raises(http.ReadError) as captured:
                await anext(iterator)
            assert captured.value is failure
        else:
            second = await anext(iterator)
            assert (second.delta.text if provider == "anthropic" else second.choices[0].delta.content) == "second"
        await stream.close()
        assert reads == ["first", "second"]
        assert closed == [True]
    assert sdk.is_closed()
