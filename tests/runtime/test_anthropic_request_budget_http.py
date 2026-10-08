"""Anthropic budget reservations follow the payload received by the SDK."""

from __future__ import annotations

import copy
import json

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport

_MODEL = "claude-sonnet-4-6"
_THINKING = {"thinking": {"type": "adaptive"}}


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "false")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")


def _wire_input_tokens(request):
    messages = list(request["messages"])
    if "system" in request:
        messages.insert(0, {"role": "system", "content": request["system"]})
    return estimate_request_tokens(messages, request.get("tools"))


@pytest.mark.parametrize("budget,thinking,expected_tokens,expected_calls", [
    (11_000, True, 9702, 2),
    (None, True, 9702, 2),
    (11_000, False, 552, 2),
    (7000, True, 4700, 1),
])
async def test_long_thinking_tool_round_uses_one_native_copy(
    monkeypatch, tmp_path, budget, thinking, expected_tokens, expected_calls,
):
    requests = []
    thinking_text = "consider " * 1500
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            content = []
            if thinking:
                content.append({"type": "thinking", "thinking": thinking_text, "signature": "local-signature"})
            content.append({"type": "tool_use", "id": "call_read", "name": "file_read",
                            "input": {"path": "sample.txt"}})
            usage = {"input_tokens": 200, "output_tokens": 4500 if thinking else 50}
            stop_reason = "tool_use"
        else:
            content = [{"type": "text", "text": "ok"}]
            usage = {"input_tokens": 5000 if thinking else 300, "output_tokens": 2}
            stop_reason = "end_turn"
        return http_response(request, 200, json={
            "id": f"msg_{len(requests)}", "type": "message", "role": "assistant", "model": _MODEL,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage,
        })

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with LLMClient(
        model=_MODEL, provider="anthropic", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        agent = Agent(name="budget", system_prompt="Read sample.txt and answer ok", model=_MODEL,
                      provider="anthropic", tools=[FileReadTool()], thinking=thinking,
                      thinking_params=_THINKING if thinking else {})
        session = build_session(
            agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)),
            max_budget_tokens=budget, max_steps=3,
        )
        await session.add_user_message("Read sample.txt and answer ok")
        answer = await session.run_loop()

    assert len(requests) == expected_calls
    assert session.used_tokens == expected_tokens
    tool_result = next(message for message in session.messages if message["role"] == "tool")
    assert "alpha" in tool_result["content"]
    assistant = next(message for message in session.messages if message["role"] == "assistant")
    if thinking:
        assert assistant["reasoning_content"] == thinking_text
        assert assistant["provider_state"]["anthropic_content"][0] == {
            "type": "thinking", "thinking": thinking_text, "signature": "local-signature",
        }
    if expected_calls == 1:
        assert answer == ""
        assert session.phase is SessionPhase.STOPPED
        assert session.state.terminal_reason.startswith("budget exhausted before model call")
        return

    assert answer == "ok"
    assert session.phase is SessionPhase.DONE
    if budget is not None:
        first_round_tokens = 4700 if thinking else 250
        assert requests[1]["max_tokens"] == min(8192, budget - first_round_tokens - _wire_input_tokens(requests[1]))
        assert session.used_tokens < budget
    else:
        assert requests[1]["max_tokens"] == 8192
    replay = requests[1]["messages"][1]["content"]
    assert replay[-1] == {"type": "tool_use", "id": "call_read", "name": "file_read",
                          "input": {"path": "sample.txt"}}
    assert requests[1]["messages"][2]["content"] == [{
        "type": "tool_result", "tool_use_id": "call_read", "content": tool_result["content"],
    }]
    assert json.dumps(requests[1]).count(thinking_text) == int(thinking)


@pytest.mark.parametrize("history_shape", ["native", "legacy", "empty-native"])
async def test_reservation_counts_system_tools_and_native_blocks_received_by_sdk(
    monkeypatch, history_shape,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    tool_calls = [
        {"id": "call_read", "type": "function",
         "function": {"name": "read", "arguments": json.dumps({"path": "x" * 6000})}},
        {"id": "call_other", "type": "function", "function": {"name": "read", "arguments": "{}"}},
    ]
    blocks = [
        {"type": "thinking", "thinking": "plan " * 1200, "signature": "s" * 6000},
        {"type": "redacted_thinking", "data": "r" * 6000},
        {"type": "text", "text": "Reading now"},
        {"type": "tool_use", "id": "call_read", "name": "read", "input": {"path": "x" * 6000}},
        {"type": "tool_use", "id": "call_other", "name": "read", "input": {}},
    ]
    assistant = {"role": "assistant", "content": "Reading now", "reasoning_content": blocks[0]["thinking"],
                 "tool_calls": tool_calls}
    if history_shape != "legacy":
        assistant["provider_state"] = {"anthropic_content": blocks if history_shape == "native" else []}
    messages = [
        {"role": "system", "content": [{"type": "input_text", "text": "instructions " * 500}]},
        {"role": "system", "content": "other instructions"},
        {"role": "user", "content": "Read the file"},
        assistant,
    ]
    if history_shape != "empty-native":
        messages.extend([
            {"role": "tool", "tool_call_id": "call_read", "content": "result " * 1000},
            {"role": "tool", "tool_call_id": "call_other", "content": "another result"},
        ])
    messages.extend([
        {"role": "system", "content": "Earlier context", "compacted": True},
        {"role": "user", "content": "Continue"},
    ])
    tools = [{"type": "function", "function": {
        "name": "read", "description": "Read a file", "strict": True,
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }}]
    original = copy.deepcopy((messages, tools))
    async with LLMClient(
        model=_MODEL, provider="anthropic", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        reserved = client.estimate_request_tokens(messages, tools, thinking=True, thinking_params=_THINKING)
        await client.complete(messages, tools, thinking=True, thinking_params=_THINKING)

    assert (messages, tools) == original
    assert reserved == _wire_input_tokens(requests[0])
    assert requests[0]["system"] == "instructions " * 500 + "\n\nother instructions"
    assert requests[0]["tools"][0]["strict"] is True
    assert requests[0]["messages"][-2] == {"role": "user", "content": "Earlier context"}
    if history_shape == "native":
        assert requests[0]["messages"][1]["content"] == blocks
    elif history_shape == "empty-native":
        assert all(message["role"] != "assistant" for message in requests[0]["messages"])
    else:
        assert requests[0]["messages"][1]["content"] == blocks[2:]


async def test_reservation_handles_large_manual_thinking_configuration(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    params = {"thinking": {"type": "enabled", "budget_tokens": 16_000}}
    messages = [{"role": "user", "content": "Return ok"}]
    async with LLMClient(
        model=_MODEL, provider="anthropic", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        reserved = client.estimate_request_tokens(messages, thinking=True, thinking_params=params)
        await client.complete(messages, thinking=True, thinking_params=params, max_output_tokens=32_768)
    assert requests[0]["thinking"] == params["thinking"]
    assert reserved == _wire_input_tokens(requests[0])


@pytest.mark.parametrize("native", [False, True], ids=["legacy-message-only", "native-snapshot"])
async def test_saved_anthropic_history_reserves_the_replayed_payload(monkeypatch, tmp_path, native):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    thinking_text = "consider " * 1500
    assistant = {"role": "assistant", "content": None, "reasoning_content": thinking_text,
                 "tool_calls": [{"id": "call_read", "type": "function",
                                 "function": {"name": "file_read", "arguments": '{"path":"sample.txt"}'}}]}
    if native:
        assistant["provider_state"] = {"anthropic_content": [
            {"type": "thinking", "thinking": thinking_text, "signature": "local-signature"},
            {"type": "tool_use", "id": "call_read", "name": "file_read", "input": {"path": "sample.txt"}},
        ]}
    history = [{"role": "user", "content": "Read sample.txt"}, assistant,
               {"role": "tool", "tool_call_id": "call_read", "content": "alpha\n"}]
    async with LLMClient(
        model=_MODEL, provider="anthropic", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        agent = Agent(name="restore", system_prompt="Read the file and answer", model=_MODEL,
                      provider="anthropic", tools=[FileReadTool()], thinking=True, thinking_params=_THINKING)
        source = build_session(agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)))
        source.messages = history
        path = tmp_path / "saved.json"
        if native:
            source.state.set_used_tokens(4700)
            source.save(str(path))
        else:
            source.store.save(str(path), history)
        budget = 11_000 if native else 1500
        loaded = load_session(str(path), agent=agent, llm=client, max_budget_tokens=budget)
        assert loaded.used_tokens == (4700 if native else 0)
        assert await loaded.run_loop() == "ok"
        assert loaded.phase is SessionPhase.DONE
        assert loaded.used_tokens == (4704 if native else 4)

    assert len(requests) == 1
    assert requests[0]["max_tokens"] == budget - (4700 if native else 0) - _wire_input_tokens(requests[0])
    replay = requests[0]["messages"][1]["content"]
    assert any(block["type"] == "thinking" for block in replay) is native
    assert replay[-1] == {"type": "tool_use", "id": "call_read", "name": "file_read",
                          "input": {"path": "sample.txt"}}
    assert requests[0]["messages"][2]["content"][0]["content"] == "alpha\n"
