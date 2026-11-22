"""Visible Chat refusal text through the real SDK and public agent runtime."""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from opencollab import OpenCollab, RunError
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.errors import StreamedUsageUnavailableError
from opencollab.tools import builtin_tools

_REFUSAL = "I cannot provide that response. I can explain the general principles."
_ANSWER = "Here are the general principles."
_USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}
_TOOL_CALL = {"id": "read-1", "type": "function", "function": {
    "name": "file_read", "arguments": '{"path":"facts.txt"}',
}}
_TOOL_MARKUP = (
    "<|tool_calls_section_begin|><|tool_call_begin|>functions.file_read:read-1"
    '<|tool_call_argument_begin|>{"path":"facts.txt"}<|tool_call_end|><|tool_calls_section_end|>'
)

def _reply(stream, content, refusal, finish_reason, *, reasoning=None, tool_calls=None, usage=_USAGE):
    common = {"id": "chatcmpl-refusal", "created": 1, "model": "gpt-4o"}
    if not stream:
        message = {"role": "assistant", "content": content, "refusal": refusal}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return httpx.Response(200, json={
            **common,
            "object": "chat.completion",
            **({"usage": usage} if usage is not None else {}),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
        })

    common["object"] = "chat.completion.chunk"
    deltas = [{"role": "assistant", "content": content if not content else "", "refusal": None}]
    for field, text in (("content", content), ("refusal", refusal), ("reasoning_content", reasoning)):
        if text:
            deltas.extend({field: text[offset:offset + 7]} for offset in range(0, len(text), 7))
    if tool_calls:
        deltas.append({"tool_calls": [{"index": index, **call} for index, call in enumerate(tool_calls)]})
    frames = [
        {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    frames.extend([
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
        {**common, "choices": [], "usage": usage},
    ])
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body.encode())

async def _client(stream, handler):
    client = LLMClient(model="gpt-4o", api_key="unused", max_retries=0, stream_chat=stream)  # pragma: allowlist secret
    await client._openai.close()
    client._openai = openai.AsyncOpenAI(
        api_key="unused",  # pragma: allowlist secret
        base_url="https://refusal.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client

async def _run_reply(tmp_path, stream, content, refusal, finish_reason="stop", *, tool_calls=None):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return _reply(stream, content, refusal, finish_reason, tool_calls=tool_calls)

    client = await _client(stream, handler)
    artifacts = tmp_path / "artifacts"
    try:
        result = await OpenCollab(
            tmp_path, model="gpt-4o", provider="openai", api_key="unused",  # pragma: allowlist secret
            config={"thinking": False, "top_p": None},
        ).agent(
            "Return a brief visible response.", llm=client,
            tools=builtin_tools("file_read") if tool_calls else (),
            max_steps=3, budget=10_000, timeout=10, cleanup_timeout=1,
            trace=False, artifacts=artifacts,
        )
        history = json.loads((artifacts / "agent.json").read_text())["messages"]
        return result, requests, history
    finally:
        await client.close()

@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("content", [None, "", " \t\n"], ids=["absent", "empty", "whitespace"])
async def test_refusal_only_reply_is_returned_and_persisted_once(tmp_path, stream, content):
    result, requests, history = await _run_reply(tmp_path, stream, content, _REFUSAL)

    assert result.output == _REFUSAL
    assert result.status == "completed"
    assert result.tokens == 20
    assert len(requests) == 1
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == _REFUSAL

@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("refusal", [None, _REFUSAL], ids=["content", "mixed"])
async def test_visible_content_keeps_precedence_over_refusal(tmp_path, stream, refusal):
    result, requests, history = await _run_reply(tmp_path, stream, _ANSWER, refusal)

    assert result.output == _ANSWER
    assert result.tokens == 20
    assert len(requests) == 1
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == _ANSWER

@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("content", [None, "An unfinished answer"], ids=["empty", "partial"])
async def test_filtered_reply_stops_without_retry(tmp_path, stream, content):
    result, requests, history = await _run_reply(tmp_path, stream, content, None, "content_filter")

    assert result.output == (content or "")
    assert result.status == "stopped"
    assert not result.ok
    assert result.reason == "provider filtered the output"
    with pytest.raises(RunError, match="provider filtered the output"):
        result.raise_for_status()
    assert result.tokens == 20
    assert len(requests) == 1
    assert [row["content"] for row in history if row["role"] == "assistant"] == ([content] if content else [])

@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_filtered_reply_does_not_execute_or_persist_interrupted_tool_call(tmp_path, stream):
    interrupted = {**_TOOL_CALL, "function": {"name": "file_re", "arguments": '{"path":'}}
    result, requests, history = await _run_reply(
        tmp_path, stream, "Partial answer", None, "content_filter", tool_calls=[interrupted],
    )

    assert result.status == "stopped"
    assert result.output == "Partial answer"
    assert result.reason == "provider filtered the output"
    assert result.tokens == 20
    assert len(requests) == 1
    assert not any(row["role"] == "tool" or row.get("tool_calls") for row in history)

@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize(
    ("content", "refusal", "reasoning", "calls", "expected_content", "expected_calls", "recovered"),
    [
        (None, _TOOL_MARKUP, None, None, _TOOL_MARKUP, [], 0),
        (None, _REFUSAL, _TOOL_MARKUP, None, _REFUSAL, [], 0),
        (None, _TOOL_MARKUP, None, [_TOOL_CALL], _TOOL_MARKUP, [_TOOL_CALL], 0),
        (_TOOL_MARKUP, _REFUSAL, None, None, None, [_TOOL_CALL], 1),
    ],
    ids=["refusal-markup", "reasoning-markup", "structured", "content-markup"],
)
async def test_refusal_stays_text_while_explicit_content_and_structured_calls_keep_their_tools(
    stream, content, refusal, reasoning, calls, expected_content, expected_calls, recovered,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return _reply(
            stream, content, refusal, "tool_calls" if calls else "stop", reasoning=reasoning, tool_calls=calls,
        )

    client = await _client(stream, handler)
    try:
        response = await client.complete([{"role": "user", "content": "Explain the general principles."}])
    finally:
        await client.close()

    assert response.content == expected_content
    assert response.tool_calls == expected_calls
    assert response.usage.markup_recovered == recovered
    assert response.usage.total_tokens == 20
    assert len(requests) == 1

@pytest.mark.parametrize(("stream", "usage"), [
    (False, None),
    (False, {"prompt_tokens": 13, "completion_tokens": 0, "total_tokens": 13}),
])
@pytest.mark.parametrize("extras", [
    {}, {"reasoning": "Consider the permitted alternative."}, {"tool_calls": [_TOOL_CALL]},
])
async def test_refusal_output_estimate_matches_the_same_visible_content(stream, usage, extras):
    async def parsed(content, refusal):
        async def handler(_request):
            return _reply(stream, content, refusal, "stop", usage=usage, **extras)

        client = await _client(stream, handler)
        try:
            return await client.complete([{"role": "user", "content": "Explain the available alternative."}])
        finally:
            await client.close()

    ordinary = await parsed(_REFUSAL, None)
    refused = await parsed(None, _REFUSAL)
    mixed = await parsed(_REFUSAL, "A separate refusal field.")

    assert ordinary.content == refused.content == mixed.content == _REFUSAL
    assert ordinary.usage.estimated and refused.usage.estimated and mixed.usage.estimated
    assert refused.usage.input_tokens == ordinary.usage.input_tokens == mixed.usage.input_tokens
    assert refused.usage.output_tokens == ordinary.usage.output_tokens == mixed.usage.output_tokens
    assert refused.usage.output_tokens > 10

@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 13, "completion_tokens": 0, "total_tokens": 13}])
async def test_streamed_refusal_still_requires_reported_usage(usage):
    async def handler(_request):
        return _reply(True, None, _REFUSAL, "stop", usage=usage)

    client = await _client(True, handler)
    try:
        with pytest.raises(StreamedUsageUnavailableError):
            await client.complete([{"role": "user", "content": "Explain the available alternative."}])
    finally:
        await client.close()

async def test_estimation_preserves_the_original_refusal_payload():
    from opencollab.adapters.llm._chat_response import _estimate_output_tokens

    message = {"role": "assistant", "content": None, "refusal": _REFUSAL}
    original = dict(message)
    expected = _estimate_output_tokens({"role": "assistant", "content": _REFUSAL})

    assert _estimate_output_tokens(message) == expected
    assert message == original
