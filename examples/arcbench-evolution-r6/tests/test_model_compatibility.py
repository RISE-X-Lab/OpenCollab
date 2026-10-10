"""Competition reasoning and tool-choice behavior on real local HTTP requests."""

import pytest
from arcbench_r6.model import CompetitionModel

from opencollab.adapters.llm.client import LLMClient
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.runtime.test_llm_chat_streaming_http import fake_chat_server

MESSAGES = [
    {"role": "system", "content": "Exact competition system"},
    {"role": "user", "content": "Inspect the application"},
    {
        "role": "assistant",
        "content": None,
        "reasoning_content": "Recorded reasoning for continuation",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "inspect", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "call-1", "content": "observed application"},
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "inspect",
            "description": "Inspect the application",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,expected",
    [("required", "auto"), ("none", "none"), ({"type": "none"}, "none"), ({"type": "any"}, "auto"),
     ({"type": "function", "function": {"name": "inspect"}}, "auto")],
)
async def test_actual_chat_request_and_input_estimate_agree(choice, expected):
    with fake_chat_server() as (base_url, requests):
        client = LLMClient(
            model="deepseek-competition-example",
            provider="openai",
            api_key="fixture-key",  # pragma: allowlist secret
            base_url=base_url,
            max_retries=0,
        )
        wrapped = CompetitionModel(client)
        try:
            predicted = wrapped.estimate_request_tokens(MESSAGES, TOOLS, thinking=False)
            response = await wrapped.complete(
                MESSAGES, TOOLS, thinking=False, tool_choice=choice, max_output_tokens=128
            )
        finally:
            await client.close()
    assert response.content == "391"
    assert len(requests) == 1
    body = requests[0]
    assert body["tool_choice"] == expected
    assert body["messages"][0] == MESSAGES[0]
    assert body["messages"][2]["reasoning_content"] == MESSAGES[2]["reasoning_content"]
    assert "thinking" not in body and "enable_thinking" not in body
    assert predicted == estimate_request_tokens(body["messages"], body["tools"])


@pytest.mark.asyncio
async def test_explicit_thinking_disable_is_preserved():
    with fake_chat_server() as (base_url, requests):
        client = LLMClient(
            model="deepseek-competition-example",
            provider="openai",
            api_key="fixture-key",  # pragma: allowlist secret
            base_url=base_url,
            max_retries=0,
        )
        wrapped = CompetitionModel(client)
        try:
            settings = {"enable_thinking": False}
            predicted = wrapped.estimate_request_tokens(MESSAGES, TOOLS, thinking=True, thinking_params=settings)
            await wrapped.complete(MESSAGES, TOOLS, thinking=True, thinking_params=settings, max_output_tokens=128)
        finally:
            await client.close()
    body = requests[0]
    assert body["enable_thinking"] is False
    assert "reasoning_content" not in body["messages"][2]
    assert predicted == estimate_request_tokens(body["messages"], body["tools"])
