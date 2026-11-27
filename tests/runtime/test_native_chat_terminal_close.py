from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm import retry
from tests.support.provider_sdk_http import install_sdk_transport, provider_http


@pytest.mark.parametrize("scene", [
    "healthy", "close-reset", "paid-read-reset", "read-and-close-reset", "no-finish", "no-usage", "cancel-close",
])
async def test_native_chat_close_error_is_distinct_from_an_unfinished_read(tmp_path, monkeypatch, scene):
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_path))
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry, "RETRY_JITTER_MAX_SECONDS", 0.0)
    close_started = asyncio.Event()
    close_cancelled = asyncio.Event()
    requests, closes, observed_usage, cancellations = [], [], [], []
    http = provider_http("openai")

    def chunk(**data):
        common = {"id": "reply", "object": "chat.completion.chunk", "model": "gpt-4o", "created": 1}
        return f"data: {json.dumps({**common, **data})}\n\n".encode()

    class Reply(http.AsyncByteStream):
        def __init__(self, attempt):
            self.attempt = attempt
            self.mode = scene if attempt == 1 else "healthy"

        async def __aiter__(self):
            answer = "First answer" if self.attempt == 1 else "Retried answer"
            finish_reason = None if self.mode in {"read-and-close-reset", "no-finish"} else "stop"
            yield chunk(choices=[{"index": 0, "delta": {"content": answer}, "finish_reason": finish_reason}])
            if self.mode == "read-and-close-reset":
                raise http.ReadError("read reset")
            if self.mode != "no-usage":
                yield chunk(choices=[], usage={"prompt_tokens": 24, "completion_tokens": 7, "total_tokens": 31})
            if self.mode == "paid-read-reset":
                raise http.ReadError("read reset after usage")
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            closes.append(self.attempt)
            if self.mode == "cancel-close":
                close_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    close_cancelled.set()
                    raise
            if self.mode in {"close-reset", "read-and-close-reset", "no-finish", "no-usage"}:
                raise http.ReadError("close reset")

    def respond(request):
        body = json.loads(request.content)
        assert body["stream_options"] == {"include_usage": True}
        requests.append(request.url.path)
        return http.Response(200, headers={"content-type": "text/event-stream"}, stream=Reply(len(requests)))

    install_sdk_transport(monkeypatch, "openai", respond)
    app = OpenCollab(
        tmp_path, provider="openai", model="gpt-4o", api_key=tmp_path.name, base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": True,
                "llm_max_retries": int(scene in {"close-reset", "paid-read-reset"})},
    )
    async with app.create_model_client() as client:
        messages = [{"role": "user", "content": "Report the result."}]

        async def complete():
            try:
                return await client.complete(messages, on_usage=lambda usage: observed_usage.append(usage.total_tokens))
            except asyncio.CancelledError as exc:
                cancellations.append(exc.args)
                raise

        if scene == "cancel-close":
            task = asyncio.create_task(complete())
            try:
                await asyncio.wait_for(close_started.wait(), timeout=2)
                task.cancel("withdraw reply")
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert close_cancelled.is_set()
            assert cancellations == [("withdraw reply",)]
        elif scene in {"read-and-close-reset", "no-finish", "no-usage"}:
            message = "read reset" if scene == "read-and-close-reset" else "close reset"
            with pytest.raises(http.ReadError, match=f"^{message}$"):
                await complete()
        else:
            response = await complete()
            assert response.content == ("Retried answer" if scene == "paid-read-reset" else "First answer")

    expected_usage = {"paid-read-reset": 62, "read-and-close-reset": 0, "no-usage": 0}.get(scene, 31)
    assert observed_usage == ([expected_usage] if expected_usage else [])
    assert closes == ([1, 2] if scene == "paid-read-reset" else [1])
    ledger, = map(json.loads, usage_path.read_text().splitlines())
    assert ledger["usage"]["total_tokens"] == expected_usage
    assert ledger["status"] == ("success" if scene in {"healthy", "close-reset", "paid-read-reset"} else "error")
