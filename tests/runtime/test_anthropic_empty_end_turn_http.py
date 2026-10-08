"""Native empty end_turn recovery through the SDK and public agent runtime."""

from __future__ import annotations

import json

import anthropic
import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.application.session_run import _EMPTY_STOP_NUDGE
from tests.support.provider_sdk_http import http_response, provider_http

_ANSWER = "The project fact is cobalt-17."
_TEXT = [{"type": "text", "text": _ANSWER}]
_EMPTY_CONTENTS = [[], [{"type": "text", "text": ""}]]
_READ = [{"type": "tool_use", "id": "toolu-read", "name": "file_read", "input": {"path": "facts.txt"}}]


def _reply(content, stop_reason="end_turn", input_tokens=13):
    return {
        "id": "msg-empty-turn", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 7 if content and content != _EMPTY_CONTENTS[1] else 2},
    }


async def _run_native(tmp_path, replies, *, tools=(), max_steps=5, budget=10_000):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(replies)
        return http_response(request, 200, json=replies[len(requests) - 1])

    client = LLMClient(
        model="claude-sonnet-4-5", provider="anthropic", api_key="unused",  # pragma: allowlist secret
        max_retries=0,
    )
    await client._anthropic.close()
    http = provider_http("anthropic")
    client._anthropic = anthropic.AsyncAnthropic(
        api_key="unused", base_url="https://empty-turn.invalid", max_retries=0,  # pragma: allowlist secret
        http_client=http.AsyncClient(transport=http.MockTransport(handler)),
    )
    (tmp_path / "facts.txt").write_text(_ANSWER + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    try:
        result = await OpenCollab(
            tmp_path, model="claude-sonnet-4-5", provider="anthropic", api_key="unused",  # pragma: allowlist secret
            config={"thinking": False, "top_p": None},
        ).agent(
            "Read facts.txt and state the project fact." if tools else "Return a brief visible answer.",
            llm=client, tools=tools, max_steps=max_steps, budget=budget,
            timeout=10, cleanup_timeout=1, trace=True, artifacts=artifacts,
        )
        history = json.loads((artifacts / "agent.json").read_text())["messages"]
        trace = [json.loads(line) for line in (artifacts / "trajectory.jsonl").read_text().splitlines()]
        return result, requests, history, trace
    finally:
        await client.close()


def _retries(trace):
    return [row["payload"] for row in trace if row["type"] == "empty_stop_retry"]


def _blocks(message):
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


@pytest.mark.parametrize("content", _EMPTY_CONTENTS, ids=["empty-list", "empty-text"])
@pytest.mark.parametrize("with_tool", [False, True], ids=["answer", "tool-answer"])
async def test_empty_end_turn_uses_one_existing_correction(tmp_path, content, with_tool):
    replies = [_reply(_READ, "tool_use")] if with_tool else []
    replies.extend([_reply(content), _reply(_TEXT)])
    result, requests, history, trace = await _run_native(
        tmp_path, replies, tools=[FileReadTool()] if with_tool else (),
    )

    assert result.output == _ANSWER
    assert result.status == "completed"
    assert result.tokens == (55 if with_tool else 35)
    assert len(requests) == (3 if with_tool else 2)
    assert _retries(trace) == [{"finish_reason": "end_turn", "had_reasoning": False}]
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == _ANSWER
    assert any(
        _EMPTY_STOP_NUDGE in block.get("text", "")
        for message in requests[-1]["messages"]
        for block in _blocks(message)
        if isinstance(block, dict)
    )
    finishes = [row["payload"]["finish_reason"] for row in trace if row["type"] == "llm_call"]
    assert finishes == (["tool_use"] if with_tool else []) + ["end_turn", "end_turn"]
    if with_tool:
        tool_results = [
            block for message in requests[1]["messages"] for block in _blocks(message)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu-read"
        assert _ANSWER in tool_results[0]["content"]


async def test_two_empty_end_turns_finish_after_one_correction(tmp_path):
    result, requests, _history, trace = await _run_native(tmp_path, [_reply([]), _reply([])])

    assert result.output == ""
    assert result.status == "completed"
    assert result.tokens == 30
    assert len(requests) == 2
    assert _retries(trace) == [{"finish_reason": "end_turn", "had_reasoning": False}]


async def test_nonempty_end_turn_finishes_without_correction(tmp_path):
    result, requests, _history, trace = await _run_native(tmp_path, [_reply(_TEXT)])

    assert result.output == _ANSWER
    assert result.status == "completed"
    assert result.tokens == 20
    assert len(requests) == 1
    assert _retries(trace) == []


@pytest.mark.parametrize(
    ("max_steps", "budget", "input_tokens", "reason"),
    [(1, 10_000, 13, "step limit reached"), (5, 1995, 1993, "budget exceeded")],
    ids=["step-limit", "budget-limit"],
)
async def test_empty_end_turn_correction_obeys_runtime_limits(tmp_path, max_steps, budget, input_tokens, reason):
    result, requests, _history, trace = await _run_native(
        tmp_path, [_reply([], input_tokens=input_tokens)], max_steps=max_steps, budget=budget,
    )

    assert result.output == ""
    assert result.status == "stopped"
    assert reason in str(result.reason)
    assert result.tokens == input_tokens + 2
    assert len(requests) == 1
    assert _retries(trace) == [{"finish_reason": "end_turn", "had_reasoning": False}]


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn", "stop_sequence"])
async def test_other_native_stop_reasons_keep_their_existing_completion(tmp_path, stop_reason):
    result, requests, _history, trace = await _run_native(tmp_path, [_reply([], stop_reason)])

    assert result.output == ""
    assert result.status == ("stopped" if stop_reason == "max_tokens" else "completed")
    assert result.tokens == 15
    assert len(requests) == 1
    assert _retries(trace) == []
    assert next(row["payload"]["finish_reason"] for row in trace if row["type"] == "llm_call") == stop_reason
