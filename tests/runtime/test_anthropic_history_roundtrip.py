"""Standard assistant histories survive native Anthropic tool rounds."""

import json

import pytest

from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import http_response, install_sdk_transport


@pytest.mark.parametrize("history_shape", ["null", "omitted", "provider_state"])
async def test_anthropic_tool_response_replays_standard_assistant_history(monkeypatch, history_shape):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        content = (
            [{"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}]
            if len(requests) == 1
            else [{"type": "text", "text": "done"}]
        )
        return http_response(request, 200, json={
            "id": "msg_roundtrip", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-6", "content": content,
            "stop_reason": "tool_use" if len(requests) == 1 else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 25, "output_tokens": 10},
        })

    install_sdk_transport(monkeypatch, "anthropic", handler)
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    async with LLMClient(
        model="claude-sonnet-4-6", provider="anthropic", api_key="test-placeholder",  # pragma: allowlist secret
        base_url="https://provider.invalid/v1", max_retries=0,
    ) as client:
        history = [{"role": "user", "content": "look it up"}]
        first = await client.complete(history, tools=[tool])
        assert first.content is None
        assistant = {"role": "assistant", "content": first.content, "tool_calls": first.tool_calls}
        if history_shape == "omitted":
            assistant.pop("content")
        elif history_shape == "provider_state":
            assistant["provider_state"] = first.provider_state
        history += [assistant, {"role": "tool", "tool_call_id": "toolu_lookup", "content": "42"}]
        second = await client.complete(history, tools=[tool])

    assert second.content == "done"
    assert len(requests) == 2
    assert requests[1]["messages"][1]["content"] == [
        {"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}
    ]
    assert requests[1]["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_lookup", "content": "42"}
    ]


@pytest.mark.parametrize("role,content", [("user", None), ("assistant", {"text": "invalid"})])
def test_anthropic_history_retains_content_validation(role, content):
    with pytest.raises(ValueError, match="content must be text or a content block list"):
        convert_to_anthropic_messages([{"role": role, "content": content}])
