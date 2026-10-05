"""Legacy saved tool exchanges resume through real provider SDK requests."""

from __future__ import annotations

import json

import httpx
import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.base import Tool
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport


def _save_session(session, path, snapshot_format):
    if snapshot_format == "current-json":
        session.save(str(path))
        return
    messages = session.state.enriched_messages()
    if snapshot_format == "legacy-jsonl":
        path.write_text("".join(json.dumps(message) + "\n" for message in messages), encoding="utf-8")
        return
    # Historical Session.save wrote these fields before session_state existed.
    session.store.save(str(path), messages, meta={
        "aid": session.state.aid,
        "role": session.agent.name,
        "model": session.agent.model,
    })


def _tool_request_response(request):
    model = json.loads(request.content)["model"]
    if request.url.path.endswith("/messages"):
        return httpx.Response(200, json={
            "id": "msg_saved_tool", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "tool_use", "id": "reused-call", "name": "saved_write", "input": {}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 2, "output_tokens": 2},
        })
    return httpx.Response(200, json={
        "id": "chatcmpl_saved_tool", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{
            "id": "reused-call", "type": "function",
            "function": {"name": "saved_write", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    })


def _tool_exchange_error(messages, provider):
    pending = []
    for message in messages:
        if provider == "anthropic":
            content = message["content"]
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for block in blocks:
                if block["type"] == "tool_result":
                    if block["tool_use_id"] not in pending:
                        return "tool_result has no preceding tool_use"
                    pending.remove(block["tool_use_id"])
                else:
                    if pending:
                        return "tool_use must be followed immediately by tool_result"
                    if block["type"] == "tool_use":
                        pending.append(block["id"])
        elif message["role"] == "tool":
            if message["tool_call_id"] not in pending:
                return "tool result has no preceding tool call"
            pending.remove(message["tool_call_id"])
        else:
            if pending:
                return "tool calls must be followed immediately by tool results"
            pending.extend(call["id"] for call in message.get("tool_calls", []))
    return "tool calls have missing results" if pending else None


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("snapshot_format", ["legacy-json", "legacy-jsonl", "current-json"])
@pytest.mark.parametrize("closed", [False, True], ids=["interrupted", "completed"])
async def test_saved_tool_exchange_can_continue_without_replaying_effects(
    monkeypatch, tmp_path, provider, snapshot_format, closed,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        error = _tool_exchange_error(body["messages"], provider)
        if error:
            return httpx.Response(400, json={
                "type": "error", "error": {"type": "invalid_request_error", "message": error},
            })
        if len(requests) <= 2:
            return _tool_request_response(request)
        return completion_http_response(request, text="continued answer")

    install_sdk_transport(monkeypatch, provider, handler)
    path = tmp_path / ("saved.jsonl" if snapshot_format == "legacy-jsonl" else "saved.json")
    effects = tmp_path / "effects.txt"

    class SavedWrite(Tool):
        name = "saved_write"
        description = "Write one effect and preserve the running session."
        executions = 0
        session = None

        async def execute_with_runtime(self, args, runtime):
            self.executions += 1
            with effects.open("a", encoding="utf-8") as handle:
                handle.write(f"effect {self.executions}\n")
            if self.executions == 2 and not closed:
                _save_session(self.session, path, snapshot_format)
            return f"completed effect {self.executions}"

    tool = SavedWrite()
    model = "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o"
    agent = Agent(name="restore", system_prompt="Perform the requested writes.",
                  model=model, provider=provider, tools=[tool])
    # The placeholder credential is received by the in-memory MockTransport.
    async with LLMClient(
        model=model, provider=provider,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        source = build_session(agent=agent, llm=client, aid=7)
        tool.session = source
        await source.add_user_message("Perform two writes.")
        assert await source.run_loop() == "continued answer"
        if closed:
            _save_session(source, path, snapshot_format)
        original_effects = effects.read_text(encoding="utf-8")
        assert original_effects == "effect 1\neffect 2\n"

        loaded = load_session(str(path), agent=agent, llm=client)
        if snapshot_format != "current-json":
            assert loaded.phase is SessionPhase.IDLE
            assert loaded.used_tokens == loaded.step_count == loaded.state.context_tokens == 0
        else:
            assert loaded.used_tokens == (12 if closed else 8)
            assert loaded.step_count == (3 if closed else 2)
            assert loaded.phase is (SessionPhase.DONE if closed else SessionPhase.IDLE)
        restored_results = [message for message in loaded.messages if message["role"] == "tool"]
        await loaded.add_user_message("Continue the saved task.")
        assert await loaded.run_loop() == "continued answer"

    assert len(requests) == 4
    assert _tool_exchange_error(requests[-1]["messages"], provider) is None
    assert loaded._open_tool_call_ids() == []
    assert loaded.state.pending_events.is_empty()
    assert [message["tool_call_id"] for message in restored_results] == ["reused-call", "reused-call"]
    assert [message["content"] for message in restored_results] == [
        "completed effect 1",
        "completed effect 2" if closed else "Tool execution interrupted by session restore.",
    ]
    assert effects.read_text(encoding="utf-8") == original_effects
    assert tool.executions == 2
