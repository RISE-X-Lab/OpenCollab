"""Provider truncation retains history until a complete summary can be cached."""

from __future__ import annotations

import copy
import json

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from tests.support.provider_sdk_http import http_response, install_sdk_transport
from tests.support.responses_provider_test_support import message_item

REQUIREMENT = "Preserve the SQLite backup before replacing the database."


_CHAT = {"provider": "openai", "wire_protocol": "chat_completions", "model": "gpt-4o"}
_RESPONSES = {"provider": "openai", "wire_protocol": "responses", "model": "gpt-4o"}
_ANTHROPIC = {"provider": "anthropic", "wire_protocol": "chat_completions", "model": "claude-sonnet-4-6"}

@pytest.mark.parametrize(("configuration", "interruption"), [
    pytest.param(_CHAT, None, id="complete-chat"),
    pytest.param(_CHAT, "length", id="truncated-chat"),
    pytest.param(_CHAT, "content_filter", id="filtered-chat"),
    pytest.param(_RESPONSES, None, id="complete-responses"),
    pytest.param(_RESPONSES, "length", id="truncated-responses"),
    pytest.param(_ANTHROPIC, None, id="complete-anthropic"),
    pytest.param(_ANTHROPIC, "length", id="truncated-anthropic"),
])
async def test_native_summary_completion_preserves_history_usage_and_cache(
    tmp_path, monkeypatch, configuration, interruption,
):
    truncated = interruption is not None
    requests = []
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        summary = "create a detailed summary of the conversation" in json.dumps(body)
        partial = summary and truncated and len(requests) == 1
        text = (
            "<summary>Earlier setup notes." if partial else f"<summary>{REQUIREMENT}</summary>"
        ) if summary else ("Backup preserved." if REQUIREMENT in json.dumps(body) else "Backup missing.")
        output_tokens = 1_024 if partial else 10
        if configuration["provider"] == "anthropic":
            return http_response(request, 200, json={
                "id": "msg_summary", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": text}],
                "stop_reason": "max_tokens" if partial else "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": output_tokens},
            })
        if configuration["wire_protocol"] == "responses":
            item = message_item(text)
            item["status"] = "incomplete" if partial else "completed"
            terminal = {
                "id": "resp_summary", "object": "response", "model": body["model"],
                "status": "incomplete" if partial else "completed", "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if partial else None,
                "output": [item],
                "usage": {"input_tokens": 100, "output_tokens": output_tokens, "total_tokens": 100 + output_tokens},
            }
            events = [
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.incomplete" if partial else "response.completed", "response": terminal},
            ]
            wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=wire)
        return http_response(request, 200, json={
            "id": "chat_summary", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": interruption if partial else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": output_tokens, "total_tokens": 100 + output_tokens},
        })

    install_sdk_transport(monkeypatch, configuration["provider"], handler)
    async with LLMClient(
        **configuration, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0, context_window=40_000,
    ) as client:
        session = build_session(
            agent=Agent(name="notes", system_prompt="Complete the user task.",
                        **configuration, tools=[], max_tokens_per_step=1_024),
            llm=client, env=LocalEnvironment(str(tmp_path)), max_budget_tokens=None,
        )
        original = [
            {"role": "system", "content": "Complete the user task."},
            {"role": "user", "content": "Earlier setup notes. " * 1_800 + REQUIREMENT},
            {"role": "assistant", "content": "Earlier result."},
            {"role": "user", "content": "The next work item is ready."},
            {"role": "assistant", "content": "Another result."},
            {"role": "user", "content": "Now finish the task."},
        ]
        session.messages = copy.deepcopy(original)
        try:
            assert await session.run_loop() == "Backup preserved."
            assert len(requests) == 2
            assert REQUIREMENT in json.dumps(requests[0])
            assert REQUIREMENT in json.dumps(requests[1])
            assert session.messages[:len(original)] == original
            assert session.used_tokens == (1_234 if truncated else 220)

            compact = session.runner.shaper._shapers[-1]
            assert compact.summarizer.last_call_cacheable is (not truncated)
            assert len(compact._summary_cache) == (0 if truncated else 1)
            if truncated:
                assert original[1]["content"] in json.dumps(requests[1])

            projection = await session.runner.shaper.ashape(original)
            assert REQUIREMENT in json.dumps(projection)
            assert len(requests) == (3 if truncated else 2)
            assert compact.summarizer.last_call_cacheable
            assert len(compact._summary_cache) == 1
            assert await session.runner.shaper.ashape(original) == projection
            assert len(requests) == (3 if truncated else 2)
            assert session.used_tokens == (1_344 if truncated else 220)
        finally:
            await session.aclose()
            await session.env.cleanup()
