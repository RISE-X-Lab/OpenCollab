"""End-to-end HTTP contract for the native Responses client."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.responses_messages import messages_to_input
from opencollab.domain.token_estimation import estimate_messages_tokens, estimate_tokens
from tests.support.responses_provider_test_support import message_item


def _response(response_id: str, *, include_usage: bool = True) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": 128,
        "model": "gpt-fake",
        "output": [],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": "medium", "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 0.95,
        "truncation": "disabled",
        "usage": (
            {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 7, "cache_write_tokens": 2},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 3},
                "total_tokens": 25,
            }
            if include_usage
            else None
        ),
        "metadata": {},
    }

def _item_event(item: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "sequence_number": index + 1,
        "output_index": index,
        "item": item,
    }

def _completed_event(
    response_id: str,
    sequence: int,
    output: list[dict[str, Any]],
    *,
    include_usage: bool = True,
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence_number": sequence,
        "response": {**_response(response_id, include_usage=include_usage), "output": output},
    }

# Generous deadlines distinguish protocol hangs from slow local HTTP round trips.
HANG_GUARD_SECONDS = 60

@contextmanager
def fake_responses_server(
    scripts: list[list[dict[str, Any]]],
    *,
    bearer_value: str = "fake-key",
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/responses" or self.headers.get("Authorization") != f"Bearer {bearer_value}":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(body)
            request["_user_agent"] = self.headers.get("User-Agent")
            requests.append(request)
            with lock:
                events = scripts.pop(0) if scripts else None
            if events is None:
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

@pytest.mark.asyncio
@pytest.mark.parametrize("phased", [False, True], ids=["unphased", "phased"])
async def test_real_http_stream_replays_reasoning_function_call_and_output(monkeypatch, phased):
    monkeypatch.setenv("OPENCOLLAB_LLM_USER_AGENT", "codex_cli_rs/test")
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-test-value",
    }
    call = {
        "id": "fc_1",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_exact",
        "name": "get_number",
        "arguments": '{"name":"answer"}',
    }
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "42", "annotations": []}],
    }
    commentary = {**message_item("I'll look up the number."), "id": "msg_lookup", "phase": "commentary"}
    first_items = [reasoning, commentary, call] if phased else [reasoning, call]
    scripts = [
        [
            {"type": "response.created", "sequence_number": 0, "response": _response("resp_1")},
            *(_item_event(item, index) for index, item in enumerate(first_items)),
            _completed_event("resp_1", len(first_items) + 1, first_items),
        ],
        [
            {"type": "response.created", "sequence_number": 0, "response": _response("resp_2")},
            _item_event(message, 0),
            _completed_event("resp_2", 2, [message]),
        ],
    ]
    with fake_responses_server(scripts) as (base_url, requests):
        client = LLMClient(
            model="gpt-5-fake",
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            wire_protocol="responses",
            max_retries=0,
            request_timeout=HANG_GUARD_SECONDS,
            first_event_timeout=HANG_GUARD_SECONDS,
            stream_idle_timeout=HANG_GUARD_SECONDS,
        )
        first = await client.complete(
            [{"role": "system", "content": "Use tools."}, {"role": "user", "content": "Get it."}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_number",
                    "description": "Return a number.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            temperature=1.0,
            reasoning_effort="medium",
            top_p=0.95,
            max_output_tokens=128,
            response_session_id="session-a",
        )
        second = await client.complete(
            [
                {"role": "system", "content": "Use tools."},
                {"role": "user", "content": "Get it."},
                {
                    "role": "assistant",
                    "tool_calls": first.tool_calls,
                    "response_items": first.provider_items,
                },
                {"role": "tool", "tool_call_id": "call_exact", "content": "42"},
            ],
            temperature=1.0,
            reasoning_effort="medium",
            response_session_id="session-a",
        )

        await client.close()

    assert first.content is None
    assert first.tool_calls[0]["id"] == "call_exact"
    assert second.content == "42"
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[1]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["store"] is False
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["_user_agent"] == "codex_cli_rs/test"
    assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"]
    replay = requests[1]["input"]
    assert replay[1:-1] == first_items
    assert replay[-1] == {
        "type": "function_call_output",
        "call_id": "call_exact",
        "output": "42",
    }

@pytest.mark.asyncio
async def test_real_http_missing_usage_estimates_encrypted_replay_and_registered_tools():
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "ciphertext-" * 6_000,
    }
    call = {
        "id": "fc_1",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_exact",
        "name": "lookup_record",
        "arguments": "{}",
    }
    messages = [
        {"role": "system", "content": "instruction " * 100, "response_items": []},
        {"role": "user", "content": "Look it up."},
        {"role": "assistant", "content": None, "response_items": [reasoning, call]},
        {
            "role": "tool",
            "tool_call_id": "call_exact",
            "content": "tool output " * 100,
            "response_items": [],
        },
        {"role": "user", "content": "Continue."},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup_record",
            "description": "d" * 1_200,
            "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
        },
    }]
    answer = {
        "id": "msg_done",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }
    scripts = [[
        {"type": "response.created", "sequence_number": 0, "response": _response("resp_missing_usage")},
        _item_event(answer, 0),
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {**_response("resp_missing_usage", include_usage=False), "output": [answer]},
        },
    ]]
    with fake_responses_server(scripts) as (base_url, requests):
        client = LLMClient(
            model="gpt-5-fake",
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            wire_protocol="responses",
            max_retries=0,
            request_timeout=HANG_GUARD_SECONDS,
            first_event_timeout=HANG_GUARD_SECONDS,
            stream_idle_timeout=HANG_GUARD_SECONDS,
        )
        result = await client.complete(
            messages,
            tools=tools,
            temperature=1.0,
            max_output_tokens=128,
        )
        await client.close()

    request = requests[0]
    assert reasoning in request["input"]
    assert call in request["input"]
    assert request["input"][-2] == {
        "type": "function_call_output",
        "call_id": "call_exact",
        "output": messages[3]["content"],
    }
    assert len(request["tools"]) == 1
    assert result.usage.estimated is True
    assert result.usage.raw_usage == {}
    assert result.usage.input_tokens == estimate_messages_tokens(
        messages, request["tools"], prefer_response_items=True
    )
    assert result.usage.input_tokens > estimate_messages_tokens(
        messages, prefer_response_items=True
    )
    assert result.usage.input_tokens > estimate_messages_tokens(
        [{**message, "response_items": []} for message in messages],
        request["tools"],
        prefer_response_items=True,
    )

@pytest.mark.parametrize("phased", [False, True], ids=["unphased", "phased"])
async def test_public_agent_uses_final_answer_and_replays_native_phases(tmp_path, monkeypatch, phased):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(log))
    final = message_item("391")
    items = [final]
    if phased:
        final["phase"] = "final_answer"
        items.insert(0, {
            **message_item("I'll check the multiplication."),
            "id": "msg_progress",
            "phase": "commentary",
        })
    scripts = [[
        *(_item_event(item, index) for index, item in enumerate(items)),
        _completed_event("resp_phases", len(items) + 1, items),
    ]]
    artifacts = tmp_path / "artifacts"
    with fake_responses_server(scripts, bearer_value=tmp_path.name) as (base_url, requests):
        app = OpenCollab(
            tmp_path, model="gpt-5.3-codex", api_key=tmp_path.name, base_url=base_url,
            config={"wire_protocol": "responses", "llm_max_retries": 0},
        )
        result = await app.agent(
            "What is 17 times 23?", tools=[], budget=10_000, max_steps=2,
            timeout=HANG_GUARD_SECONDS, artifacts=artifacts, trace=False,
        )

    assert (result.status, result.output, result.tokens) == ("completed", "391", 25)
    assert len(requests) == 1
    assistant = app.read_session_snapshot(artifacts / "agent.json")["messages"][-1]
    assert assistant["content"] == "391"
    assert assistant["response_items"] == items
    assert messages_to_input([assistant])[1] == items
    record, = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["status"] == "success"
    assert record["usage"]["total_tokens"] == 25
    assert record["usage"]["estimated"] is False

@pytest.mark.asyncio
@pytest.mark.parametrize("phased", [False, True], ids=["unphased", "phased"])
@pytest.mark.parametrize("include_usage", [False, True], ids=["estimated-usage", "reported-usage"])
async def test_real_http_deepseek_named_tool_uses_json_schema_text_binding(phased, include_usage):
    schema = {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "summary"],
                    "properties": {
                        "path": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            }
        },
    }
    message = {
        "id": "msg_schema",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": '{"findings":[{"path":"a.py","summary":"ok"}]}',
                "annotations": [],
            }
        ],
    }
    commentary = {
        **message_item("I'll gather the findings."),
        "id": "msg_progress",
        "phase": "commentary",
    }
    items = [message]
    if phased:
        message["phase"] = "final_answer"
        items.insert(0, commentary)
    scripts = [[
        {"type": "response.created", "sequence_number": 0, "response": _response("resp_schema")},
        *(_item_event(item, index) for index, item in enumerate(items)),
        _completed_event("resp_schema", len(items) + 1, items, include_usage=include_usage),
    ]]
    with fake_responses_server(scripts) as (base_url, requests):
        client = LLMClient(
            model="deepseek-v4-flash",
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            wire_protocol="responses",
            max_retries=0,
            request_timeout=HANG_GUARD_SECONDS,
            first_event_timeout=HANG_GUARD_SECONDS,
            stream_idle_timeout=HANG_GUARD_SECONDS,
        )
        result = await client.complete(
            [{"role": "user", "content": "Return findings."}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "structured_output",
                    "description": "Return the findings.",
                    "parameters": schema,
                },
            }],
            temperature=1.0,
            tool_choice={
                "type": "function",
                "function": {"name": "structured_output"},
            },
        )
        await client.close()

    request = requests[0]
    assert request["text"]["format"]["schema"] == schema
    assert request["text"]["format"]["strict"] is True
    assert "tools" not in request
    assert "tool_choice" not in request
    assert request["reasoning"] == {"effort": "none"}
    assert result.content is None
    assert result.tool_calls[0]["function"]["name"] == "structured_output"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {
        "findings": [{"path": "a.py", "summary": "ok"}],
    }
    assert result.provider_items[-1]["type"] == "function_call"
    assert result.provider_items[:-1] == ([commentary] if phased else [])
    if include_usage:
        assert result.usage.total_tokens == 25
        assert result.usage.estimated is False
    else:
        function = result.tool_calls[0]["function"]
        prefix = commentary["content"][0]["text"] if phased else ""
        assert result.usage.output_tokens == estimate_tokens(prefix + function["name"] + function["arguments"])
        assert result.usage.estimated is True
