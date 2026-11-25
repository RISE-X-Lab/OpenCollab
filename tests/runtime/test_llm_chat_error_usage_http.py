"""Real SDK and Session accounting for interrupted Chat streams."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import install_sdk_transport

_MODEL = "gpt-4o"
_FIRST_USAGE = {
    "prompt_tokens": 20,
    "completion_tokens": 7,
    "total_tokens": 27,
    "prompt_tokens_details": {"cached_tokens": 8},
    "completion_tokens_details": {"reasoning_tokens": 3},
}
_LAST_USAGE = {
    "prompt_tokens": 30,
    "completion_tokens": 11,
    "total_tokens": 41,
    "prompt_tokens_details": {"cached_tokens": 10},
    "completion_tokens_details": {"reasoning_tokens": 5},
}

class ReportedUsageStream(httpx.AsyncByteStream):
    def __init__(self, usage, *, disconnect=False, text="ok"):
        self.usage = usage
        self.disconnect = disconnect
        self.text = text
        self.closed = False

    async def __aiter__(self):
        common = {
            "id": "chatcmpl_usage", "object": "chat.completion.chunk", "created": 1, "model": _MODEL,
        }
        chunks = [
            {**common, "choices": [{
                "index": 0, "delta": {"role": "assistant", "content": self.text}, "finish_reason": None,
            }]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if self.usage is not None:
            chunks.append({**common, "choices": [], "usage": self.usage})
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        if self.disconnect:
            raise httpx.ReadError("peer reset after the last received chunk")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True

def _client(*, max_retries, stream_chat=True):
    return LLMClient(
        model=_MODEL, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=max_retries, stream_chat=stream_chat,
    )

def _session(client, *, local_environment_factory):
    return build_session(
        env=local_environment_factory(),
        agent=Agent(name="worker", system_prompt="Complete the task.", model=_MODEL),
        llm=client, max_budget_tokens=50_000,
    )

@pytest.fixture
def usage_log(monkeypatch, tmp_path):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(path))
    return path

def _ledger(path):
    return [json.loads(line) for line in path.read_text().splitlines()]

@pytest.mark.parametrize(
    ("attempts", "expected_tokens", "estimated"),
    [
        ([(False, _FIRST_USAGE)], 27, False),
        ([(True, _FIRST_USAGE), (False, _LAST_USAGE)], 68, False),
        ([(True, _FIRST_USAGE), (True, _LAST_USAGE)], 68, False),
        ([(True, _FIRST_USAGE)], 27, False),
        ([(True, _FIRST_USAGE), (True, None)], 27, True),
        ([(True, None), (False, _LAST_USAGE)], 41, True),
        ([(True, None), (True, None)], 0, True),
    ],
    ids=["normal", "retry-success", "retry-exhausted", "no-retry", "known-then-unknown",
         "unknown-then-known", "all-unknown"],
)
async def test_session_charges_reported_usage_from_every_chat_attempt(
    local_environment_factory,
    monkeypatch, usage_log, attempts, expected_tokens, estimated,
):
    requests = []
    streams = []

    def handler(request):
        requests.append(json.loads(request.content))
        disconnect, usage = attempts[len(requests) - 1]
        stream = ReportedUsageStream(usage, disconnect=disconnect)
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async def no_delay(_seconds):
        return None

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", no_delay)
    async with _client(max_retries=len(attempts) - 1) as client:
        session = _session(client, local_environment_factory=local_environment_factory)
        await session.add_user_message("Reply ok.")
        if attempts[-1][0]:
            with pytest.raises(httpx.ReadError, match="peer reset") as captured:
                await session.run_loop()
            if expected_tokens:
                usage = captured.value.usage
                last_known = next(raw for _, raw in reversed(attempts) if raw is not None)
                assert (usage.context_tokens if usage.context_tokens is not None else usage.input_tokens) == (
                    last_known["prompt_tokens"]
                )
            else:
                assert not hasattr(captured.value, "usage")
        else:
            assert await session.run_loop() == "ok"
            assert session.state.context_tokens == attempts[-1][1]["prompt_tokens"]

    assert session.used_tokens == expected_tokens
    assert len(requests) == len(attempts)
    assert all(stream.closed for stream in streams)
    records = _ledger(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == ("error" if attempts[-1][0] else "success")
    assert records[0]["usage"]["total_tokens"] == expected_tokens
    assert records[0]["usage"]["estimated"] is estimated
    if expected_tokens:
        raw = records[0]["usage"]["raw_usage"]
        assert raw == (attempts[0][1] if len(attempts) == 1 else {"attempts": [usage for _, usage in attempts]})
        if len(attempts) > 1:
            assert records[0]["usage"]["cached_input_tokens"] == (
                None if estimated else sum(usage["prompt_tokens_details"]["cached_tokens"] for _, usage in attempts)
            )
            assert records[0]["usage"]["reasoning_tokens"] == (
                None if estimated else sum(
                    usage["completion_tokens_details"]["reasoning_tokens"] for _, usage in attempts
                )
            )

async def test_cancelling_chat_retry_backoff_retains_reported_usage(local_environment_factory, monkeypatch, usage_log):
    entered_backoff = asyncio.Event()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=ReportedUsageStream(_FIRST_USAGE, disconnect=True),
        )

    async def wait_for_cancellation(_seconds):
        entered_backoff.set()
        await asyncio.Event().wait()

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", wait_for_cancellation)
    async with _client(max_retries=1) as client:
        session = _session(client, local_environment_factory=local_environment_factory)
        await session.add_user_message("Reply ok.")
        run = asyncio.create_task(session.run_loop())
        await asyncio.wait_for(entered_backoff.wait(), 1)
        assert session.used_tokens == 27
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True), 1)

    assert session.used_tokens == 27
    assert len(requests) == 1
    records = _ledger(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "CancelledError"
    assert records[0]["usage"]["total_tokens"] == 27

async def test_chat_retry_preserves_successful_markup_recovery(monkeypatch, usage_log):
    requests = []
    markup = (
        '<|tool_calls_section_begin|><|tool_call_begin|>functions.read_info:call_1'
        '<|tool_call_argument_begin|>{"path":"README.md"}<|tool_call_end|><|tool_calls_section_end|>'
    )

    def handler(request):
        requests.append(request)
        first = len(requests) == 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=ReportedUsageStream(
                _FIRST_USAGE if first else _LAST_USAGE, disconnect=first, text="ok" if first else markup,
            ),
        )

    async def no_delay(_seconds):
        return None

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", no_delay)
    async with _client(max_retries=1) as client:
        response = await client.complete([{"role": "user", "content": "Read the file."}])

    assert response.tool_calls[0]["function"]["name"] == "read_info"
    assert response.usage.markup_recovered == 1
    assert response.usage.total_tokens == 68
    assert response.usage.context_tokens == 30
    assert _ledger(usage_log)[0]["usage"]["total_tokens"] == 68

async def test_nonstreaming_chat_usage_retains_its_original_shape(local_environment_factory, monkeypatch, usage_log):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl_usage", "object": "chat.completion", "created": 1, "model": _MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": _FIRST_USAGE,
        })

    install_sdk_transport(monkeypatch, "openai", handler)
    async with _client(max_retries=0, stream_chat=False) as client:
        session = _session(client, local_environment_factory=local_environment_factory)
        await session.add_user_message("Reply ok.")
        assert await session.run_loop() == "ok"

    assert "stream" not in requests[0]
    assert session.used_tokens == 27
    assert session.state.context_tokens == 20
    assert _ledger(usage_log)[0]["usage"]["raw_usage"] == _FIRST_USAGE
