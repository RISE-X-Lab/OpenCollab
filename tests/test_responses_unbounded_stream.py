"""Unbounded stream waits still report transport failures and cancellation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from responses_provider_test_support import FakeStream, completed_response, ns

from opencollab.adapters.llm.responses_provider import (
    ResponsesStreamInterruptedError,
    _consume_stream,
    _create_and_consume_stream,
)


class TimedOutStream(FakeStream):
    async def __anext__(self):
        raise asyncio.TimeoutError("transport timeout")


@pytest.mark.asyncio
async def test_unbounded_iterator_timeout_retains_transport_cause():
    stream = TimedOutStream([])
    with pytest.raises(ResponsesStreamInterruptedError, match="provider transport") as captured:
        await _consume_stream(stream, None, None)
    assert isinstance(captured.value.__cause__, asyncio.TimeoutError)
    assert stream.closed


@pytest.mark.asyncio
async def test_unbounded_creation_timeout_retains_transport_cause():
    async def create(**_kwargs):
        raise asyncio.TimeoutError("transport timeout")

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ResponsesStreamInterruptedError, match="provider transport") as captured:
        await _create_and_consume_stream(client, {}, None, None, "gpt-fake")
    assert isinstance(captured.value.__cause__, asyncio.TimeoutError)


@pytest.mark.asyncio
async def test_unbounded_creation_and_idle_wait_complete():
    stream = FakeStream([
        ns(type="response.created"),
        ns(type="response.completed", response=completed_response()),
    ])

    async def create(**_kwargs):
        return stream

    state = await _create_and_consume_stream(
        SimpleNamespace(responses=SimpleNamespace(create=create)), {}, None, None, "gpt-fake",
    )
    assert state.completed_response.status == "completed"
    assert stream.closed


@pytest.mark.asyncio
async def test_unbounded_stream_still_accepts_caller_cancellation():
    entered = asyncio.Event()

    class WaitingStream(FakeStream):
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    task = asyncio.create_task(_consume_stream(stream, None, None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
