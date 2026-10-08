"""Explicit disabled-tool calls stay text-only through provider compatibility."""

from __future__ import annotations

import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.tools.fs import FileWriteTool
from tests.support.provider_sdk_http import (
    completion_http_response,
    http_response,
    install_sdk_transport,
)


def _tool_reply(request, protocol):
    """Return one valid native file-write call whenever the request allows it."""
    body = json.loads(request.content)
    arguments = {"path": "answer.txt", "mode": "create", "content": "written\n"}
    usage = {"input_tokens": 8, "output_tokens": 4}
    if protocol == "responses":
        item = {"id": "fc_write", "type": "function_call", "status": "completed",
                "call_id": "call_write", "name": "file_write", "arguments": json.dumps(arguments)}
        response = {"id": "resp_write", "object": "response", "status": "completed", "model": body["model"],
                    "output": [item], "error": None, "incomplete_details": None, "usage": usage}
        events = [{"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": response}]
        text = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=text)
    if protocol == "anthropic":
        return http_response(request, 200, json={
            "id": "msg_write", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "tool_use", "id": "call_write", "name": "file_write", "input": arguments}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": usage,
        })
    return http_response(request, 200, json={
        "id": "chat_write", "object": "chat.completion", "created": 1, "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_write", "type": "function", "function": {
                "name": "file_write", "arguments": json.dumps(arguments),
            },
        }]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })


def _choice(body):
    value = body.get("tool_choice", "auto")
    return value.get("type") if isinstance(value, dict) else value


def _has_tool_result(body, protocol):
    if protocol == "responses":
        return any(item.get("type") == "function_call_output" for item in body["input"])
    if protocol == "anthropic":
        return any(item.get("type") == "tool_result" for message in body["messages"]
                   for item in (message["content"] if isinstance(message["content"], list) else []))
    return any(message.get("role") == "tool" for message in body["messages"])


async def _run(tmp_path, protocol, model, requested):
    async def flow(ctx, inputs):
        return await ctx.agent("Give the final explanation.", tools=[FileWriteTool()], tool_choice=requested)

    return await OpenCollab(
        tmp_path, model=model, provider="anthropic" if protocol == "anthropic" else "openai",
        api_key="test-key", base_url="https://fixture.invalid/v1",  # pragma: allowlist secret
        config={"wire_protocol": "responses" if protocol == "responses" else "chat_completions",
                "llm_max_retries": 0},
    ).workflow(flow, budget=50_000, max_steps=3, trace=False)


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "qwen3.8-flash"])
@pytest.mark.parametrize("requested", ["none", {"type": "none"}])
async def test_responses_capability_fallback_preserves_disabled_tools(tmp_path, monkeypatch, model, requested):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if _choice(body) != "none" and not _has_tool_result(body, "responses"):
            return _tool_reply(request, "responses")
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "openai", handler)
    result = await _run(tmp_path, "responses", model, requested)

    assert result.ok and result.output == "ok"
    assert len(requests) == 1 and requests[0]["tool_choice"] == "none"
    assert not (tmp_path / "answer.txt").exists()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic"])
@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("requested", ["none", {"type": "none"}])
async def test_rejected_disabled_tools_never_retry_with_auto(tmp_path, monkeypatch, protocol, status, requested):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if _choice(body) == "none":
            return http_response(request, status, json={"type": "error", "error": {
                "message": "Unsupported tool_choice", "type": "invalid_request_error", "param": "tool_choice",
            }})
        if not _has_tool_result(body, protocol):
            return _tool_reply(request, protocol)
        return completion_http_response(request)

    provider = "anthropic" if protocol == "anthropic" else "openai"
    install_sdk_transport(monkeypatch, provider, handler)
    model = "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o"
    result = await _run(tmp_path, protocol, model, requested)

    assert result.output is None and result.agent_failures
    assert len(requests) == 1 and _choice(requests[0]) == "none"
    assert not (tmp_path / "answer.txt").exists()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic"])
async def test_auto_still_executes_registered_tool(tmp_path, monkeypatch, protocol):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if not _has_tool_result(body, protocol):
            return _tool_reply(request, protocol)
        return completion_http_response(request)

    provider = "anthropic" if protocol == "anthropic" else "openai"
    install_sdk_transport(monkeypatch, provider, handler)
    model = "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o"
    result = await _run(tmp_path, protocol, model, "auto")

    assert result.ok and result.output == "ok"
    assert len(requests) == 2 and all(_choice(body) == "auto" for body in requests)
    assert (tmp_path / "answer.txt").read_text() == "written\n"
