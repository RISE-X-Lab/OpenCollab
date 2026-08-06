"""End-to-end HTTP contract for the native Responses client."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from opencollab.adapters.llm.client import LLMClient


def _response(response_id: str) -> dict[str, Any]:
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
        "usage": {
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 7, "cache_write_tokens": 2},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 25,
        },
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
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence_number": sequence,
        "response": {**_response(response_id), "output": output},
    }


@contextmanager
def fake_responses_server(
    scripts: list[list[dict[str, Any]]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer fake-key":
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
async def test_real_http_stream_replays_reasoning_function_call_and_output(monkeypatch):
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
    scripts = [
        [
            {"type": "response.created", "sequence_number": 0, "response": _response("resp_1")},
            _item_event(reasoning, 0),
            _item_event(call, 1),
            _completed_event("resp_1", 3, [reasoning, call]),
        ],
        [
            {"type": "response.created", "sequence_number": 0, "response": _response("resp_2")},
            _item_event(message, 0),
            _completed_event("resp_2", 2, [message]),
        ],
    ]
    with fake_responses_server(scripts) as (base_url, requests):
        client = LLMClient(
            model="gpt-fake",
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            wire_protocol="responses",
            max_retries=0,
            request_timeout=5,
            first_event_timeout=2,
            stream_idle_timeout=2,
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
        )

        await client.close()

    assert first.tool_calls[0]["id"] == "call_exact"
    assert second.content == "42"
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[1]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["store"] is False
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["_user_agent"] == "codex_cli_rs/test"
    replay = requests[1]["input"]
    assert reasoning in replay
    assert call in replay
    assert replay[-1] == {
        "type": "function_call_output",
        "call_id": "call_exact",
        "output": "42",
    }
