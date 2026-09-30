"""Thinking tool continuations through the real SDK and Session."""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase

_REASONING = "Inspect f.py before answering."
_USAGE = {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}
_CASES = [
    ("deepseek-v4.1-flash", False, {}, True),
    ("deepseek/deepseek-v4.1-flash", False, {}, True),
    ("deepseek-v4.1-flash-2026", False, {}, True),
    ("deepseek-v4.1-flash-2026-09", False, {}, True),
    ("deepseek-v4.1-flash-2026-09-21", False, {}, True),
    ("gateway/deepseek-v4.1-flash-2026-09-21", False, {}, True),
    ("deepseek-v4-flash", False, {}, True),
    ("deepseek-v4.1-flash", True, {"thinking": {"type": "enabled"}}, True),
    ("deepseek-v4.1-flash", True, {"thinking": {"type": "disabled"}}, False),
    ("gateway/deepseek-v4.1-flash-2026-09-21", True, {"enable_thinking": False}, False),
    ("gpt-4o", False, {}, False),
    ("gateway/unknown", False, {}, False),
    ("deepseek-v4.1-flash-preview", False, {}, False),
]


def _completion_response(model, stream, *, content=None, calls=None, reasoning=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = [{"index": index, **call} for index, call in enumerate(calls)] if stream else calls
    finish = "tool_calls" if calls else "stop"
    common = {"id": "chatcmpl-replay", "created": 1, "model": model}
    if not stream:
        return httpx.Response(200, json={
            **common, "object": "chat.completion", "usage": _USAGE,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        })
    common["object"] = "chat.completion.chunk"
    frames = [
        {**common, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {**common, "choices": [], "usage": _USAGE},
    ]
    content = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content.encode())


async def _run_tool_continuation(tmp_path, model, stream, thinking, params, keep):
    requests = []
    read_call = {"id": "read-1", "type": "function", "function": {
        "name": "file_read", "arguments": '{"path":"f.py"}',
    }}

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return _completion_response(model, stream, calls=[read_call], reasoning=_REASONING)
        assert len(requests) == 2
        assistant = next(message for message in body["messages"] if message["role"] == "assistant")
        if keep and assistant.get("reasoning_content") != _REASONING:
            return httpx.Response(400, json={"error": {
                "message": "Missing reasoning_content in thinking tool continuation",
                "type": "invalid_request_error", "code": "invalid_request_error",
            }})
        if not keep and "reasoning_content" in assistant:
            return httpx.Response(400, json={"error": {
                "message": "reasoning_content is unsupported when thinking is disabled",
                "type": "invalid_request_error", "code": "invalid_request_error",
            }})
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert tool["tool_call_id"] == read_call["id"]
        assert "x = 1" in tool["content"]
        return _completion_response(model, stream, content="done")

    client = LLMClient(model=model, api_key="unused", max_retries=0, stream_chat=stream)
    await client._openai.close()
    client._openai = openai.AsyncOpenAI(
        api_key=client._openai.api_key,
        base_url="https://replay.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    agent = Agent(name="coder", system_prompt="system", model=model, tools=[FileReadTool()])
    if thinking:
        agent.thinking = thinking
        agent.thinking_params = params
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    session = build_session(
        agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)),
        max_steps=3, max_budget_tokens=10_000,
    )
    await session.add_user_message("inspect the file")
    error = None
    answer = None
    try:
        answer = await session.run_loop()
    except openai.BadRequestError as exc:
        error = type(exc).__name__
    finally:
        await client.close()
    return {
        "model": model, "stream": stream, "thinking": agent.thinking, "thinking_params": params,
        "expected_replay": keep, "answer": answer, "error": error,
        "phase": session.phase.value, "used_tokens": session.used_tokens,
        "history": session.state.messages, "requests": requests,
    }


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize(("model", "thinking", "params", "keep"), _CASES)
async def test_session_replays_thinking_tool_content_on_the_sdk_wire(tmp_path, stream, model, thinking, params, keep):
    result = await _run_tool_continuation(tmp_path, model, stream, thinking, params, keep)

    assert result["error"] is None
    assert result["answer"] == "done"
    assert result["phase"] == SessionPhase.DONE.value
    assert result["used_tokens"] == 400
    assert len(result["requests"]) == 2
    recorded = next(message for message in result["history"] if message["role"] == "assistant")
    sent = next(message for message in result["requests"][1]["messages"] if message["role"] == "assistant")
    assert recorded["reasoning_content"] == _REASONING
    if keep:
        assert sent["reasoning_content"] == _REASONING
    else:
        assert "reasoning_content" not in sent
    for request in result["requests"]:
        if thinking:
            assert all(request[key] == value for key, value in params.items())
        else:
            assert "thinking" not in request
            assert "enable_thinking" not in request
