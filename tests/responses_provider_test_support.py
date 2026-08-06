from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


def ns(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


def completed_response(
    *,
    output: list[dict[str, Any]] | None = None,
    status: str = "completed",
    error: Any = None,
    incomplete_details: Any = None,
    model: str = "gpt-fake",
) -> SimpleNamespace:
    return ns(
        status=status,
        error=error,
        incomplete_details=incomplete_details,
        model=model,
        output=output or [],
        usage=ns(
            input_tokens=120,
            input_tokens_details=ns(cached_tokens=80, cache_write_tokens=12),
            output_tokens=30,
            output_tokens_details=ns(reasoning_tokens=9),
            total_tokens=150,
        ),
    )


def message_item(text: str) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_item(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


class FakeStream:
    def __init__(self, events: list[Any], *, delays: list[float] | None = None):
        self.events = iter(events)
        self.delays = iter(delays or [0.0] * len(events))
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        delay = next(self.delays, 0.0)
        if delay:
            await asyncio.sleep(delay)
        try:
            return next(self.events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self):
        self.closed = True
