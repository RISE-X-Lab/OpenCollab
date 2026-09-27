"""Chat streaming through real local HTTP and SSE transport."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from opencollab.adapters.llm.client import LLMClient
from tests.runtime.test_llm_chat_streaming import MESSAGES, MODEL, USAGE

# ---------------------------------------------------------------------------
# HTTP contract: what actually goes on the wire
# ---------------------------------------------------------------------------


def _completion_body() -> dict[str, Any]:
    return {
        "id": "chatcmpl-http",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "391"},
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
    }


def _sse_chunks() -> list[dict[str, Any]]:
    def frame(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": "chatcmpl-http",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [],
        }
        body.update(overrides)
        return body

    def delta_frame(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return frame(choices=[{"index": 0, "delta": delta, "finish_reason": finish}])

    return [
        delta_frame({"role": "assistant"}),
        delta_frame({"reasoning_content": "17*23 "}),
        delta_frame({"reasoning_content": "= 391"}),
        delta_frame({"content": "391"}),
        delta_frame({}, "stop"),
        frame(usage=USAGE),
    ]


@contextmanager
def fake_chat_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Serves /v1/chat/completions as JSON or SSE depending on the request."""
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(body)
            with lock:
                requests.append(request)
            if not request.get("stream"):
                payload = json.dumps(_completion_body()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in _sse_chunks():
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
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
async def test_switch_off_puts_no_stream_key_on_the_wire():
    """Read at the socket, not at the call site: the 60 runs already on disk are
    only usable as a control if the off path sends the same body it sent then."""
    with fake_chat_server() as (base_url, requests):
        client = LLMClient(
            model=MODEL,
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            max_retries=0,
            request_timeout=5,
        )
        response = await client.complete(MESSAGES, temperature=0.2)
        await client.close()

    assert response.content == "391"
    assert set(requests[0]) == {"messages", "model", "temperature"}


@pytest.mark.asyncio
async def test_switch_on_streams_and_records_reasoning_over_real_sse():
    with fake_chat_server() as (base_url, requests):
        client = LLMClient(
            model=MODEL,
            api_key="fake-key",  # pragma: allowlist secret
            base_url=base_url,
            max_retries=0,
            request_timeout=5,
            first_event_timeout=5,
            stream_idle_timeout=5,
            stream_chat=True,
        )
        response = await client.complete(MESSAGES, temperature=0.2)
        await client.close()

    assert set(requests[0]) == {
        "messages",
        "model",
        "temperature",
        "stream",
        "stream_options",
    }
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert response.reasoning == "17*23 = 391"
    assert response.content == "391"
    assert response.finish_reason == "stop"
    assert response.usage.estimated is False
    assert response.usage.input_tokens == 96
