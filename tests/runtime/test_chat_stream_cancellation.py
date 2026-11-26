"""Cancellation and paid usage across timed Chat stream reads."""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import install_sdk_transport, provider_http


@pytest.mark.parametrize("mode", ["timer-cancel", "header-cancel", "late-frame-cancel", "direct-cancel", "no-cancel"])
async def test_chat_usage_frame_cancel_preserves_allowance_for_another_call(tmp_path, monkeypatch, mode):
    mode_requests = []
    accepted_cancellations = []
    caller_errors = []
    reports = {"first": [], "second": []}
    stream_closures = []
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_path))
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    http = provider_http("openai")
    loop = asyncio.get_running_loop()
    caller = None
    waiting_for_late_frame = asyncio.Event()

    def cancel_first(_done=None):
        accepted_cancellations.append(caller.cancel("withdraw first answer"))

    class ChatStream(http.AsyncByteStream):
        def __init__(self, actor, attempt):
            self.actor = actor
            self.attempt = attempt
            self.disconnect = attempt == 1
            self.total = (27 if actor == "first" else 23) if self.disconnect else (41 if actor == "first" else 19)

        async def __aiter__(self):
            if self.actor == "first" and self.attempt == 1 and mode == "header-cancel":
                await asyncio.sleep(0)
            common = {"id": "chatcmpl-usage-timing", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o"}
            status = "partial" if self.disconnect else "finished"
            answer = {"role": "assistant", "content": f"{self.actor} {status}"}
            for delta, finish in [(answer, None), ({}, "stop")]:
                choice = {"index": 0, "delta": delta, "finish_reason": finish}
                yield f"data: {json.dumps({**common, 'choices': [choice]})}\n\n".encode()

            direct_iteration = False
            if self.actor == "first" and self.attempt == 1 and mode in {"timer-cancel", "direct-cancel"}:
                chunk_owner = asyncio.current_task()
                direct_iteration = chunk_owner is caller
                if direct_iteration:
                    loop.call_soon(cancel_first)
                else:
                    # The SDK has decoded the usage frame before __anext__ completes.
                    chunk_owner.add_done_callback(cancel_first)
            if self.actor == "first" and self.attempt == 1 and mode == "late-frame-cancel":
                waiting_for_late_frame.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # The local transport can finish this frame while unwinding.
                    pass
            usage = {"prompt_tokens": self.total - 7, "completion_tokens": 7, "total_tokens": self.total}
            yield f"data: {json.dumps({**common, 'choices': [], 'usage': usage})}\n\n".encode()

            if self.disconnect:
                if direct_iteration:
                    # Let the queued user cancellation arrive after the paid frame.
                    await asyncio.sleep(0)
                raise http.ReadError("peer reset after the usage frame")
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            stream_closures.append((self.actor, self.attempt))

    async def handler(request):
        body = json.loads(request.content)
        actor = body["messages"][-1]["content"]
        attempt = mode_requests.count(actor) + 1
        mode_requests.append(actor)
        if mode == "header-cancel" and actor == "first" and attempt == 1:
            opening = asyncio.current_task()
            if opening is caller:
                loop.call_soon(cancel_first)
            else:
                opening.add_done_callback(cancel_first)
        assert body["stream"] and body["stream_options"] == {"include_usage": True}
        return http.Response(200, headers={"content-type": "text/event-stream"}, stream=ChatStream(actor, attempt))

    install_sdk_transport(monkeypatch, "openai", handler)

    if mode == "direct-cancel":
        client = LLMClient(
            model="gpt-4o", api_key=tmp_path.name, base_url="https://fixture.invalid/v1",
            stream_chat=True, first_event_timeout=3, stream_idle_timeout=None,
            max_retries=1, provider_error_time_budget=1.6,
        )
    else:
        client = OpenCollab(
            tmp_path, model="gpt-4o", provider="openai", api_key=tmp_path.name,
            base_url="https://fixture.invalid/v1",
            config={"llm_stream_chat": True, "llm_first_event_timeout": 3,
                    "llm_stream_idle_timeout": 3, "llm_max_retries": 1, "provider_error_time_budget": 1.6},
        ).create_model_client()

    async with client:
        async def complete(actor):
            try:
                reply = await client.complete(
                    [{"role": "user", "content": actor}],
                    on_usage=lambda usage: reports[actor].append(usage.total_tokens),
                )
            except BaseException as exc:
                if actor == "first":
                    caller_errors.append(exc)
                raise
            return (type(reply).__name__, reply.content)

        caller = asyncio.create_task(complete("first"))
        if mode == "late-frame-cancel":
            await asyncio.wait_for(waiting_for_late_frame.wait(), 2)
            cancel_first()
        done, _ = await asyncio.wait({caller}, timeout=4)
        assert caller in done
        try:
            first = await caller
        except BaseException:
            exc, = caller_errors
            first = (type(exc).__name__, str(exc))
        after_first = client.provider_retry_budget.remaining_seconds
        try:
            second = await complete("second")
        except Exception as exc:
            second = (type(exc).__name__, str(exc))

    ledger = [json.loads(line) for line in usage_path.read_text().splitlines()]
    ledger_history = [
        (record["status"], record["usage"]["total_tokens"], (record.get("error") or {}).get("type"))
        for record in ledger
    ]
    assert len(ledger) == 2
    assert len(stream_closures) == len(mode_requests)
    if mode == "no-cancel":
        assert first == ("LLMResponse", "first finished")
        assert mode_requests == ["first", "first", "second"]
        assert reports == {"first": [68], "second": [23]}
        assert second[0] == "ReadError"
        return

    assert accepted_cancellations == [True]
    assert after_first == 1.6
    assert first == ("CancelledError", "withdraw first answer")
    assert mode_requests == ["first", "second", "second"]
    expected_first = 0 if mode == "header-cancel" else 27
    assert reports == {"first": [expected_first] if expected_first else [], "second": [42]}
    assert second == ("LLMResponse", "second finished")
    assert ledger_history == [("error", expected_first, "CancelledError"), ("success", 42, None)]
