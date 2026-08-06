"""Semantic comparison tests for streamed and terminal Responses items."""

from __future__ import annotations

import pytest
from responses_provider_test_support import (
    FakeStream,
    completed_response,
    function_item,
    message_item,
    ns,
)

from opencollab.adapters.llm.responses_provider import _consume_stream, _parse_stream


@pytest.mark.asyncio
async def test_stream_accepts_terminal_projection_without_transport_metadata():
    streamed_message = {
        **message_item("OK"),
        "phase": "final_answer",
        "metadata": {"turn_id": "turn-1"},
        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
    }
    streamed_message["content"][0]["logprobs"] = []
    final_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    }
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed_message),
            ns(
                type="response.completed",
                response=completed_response(output=[final_message]),
            ),
        ]),
        1,
        1,
        "gpt-fake",
    )

    parsed = _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")

    assert parsed.content == "OK"
    assert parsed.provider_items == [streamed_message]


@pytest.mark.asyncio
async def test_stream_accepts_terminal_function_call_projection():
    streamed_call = {
        **function_item("call_1", "read_file", '{"path":"a.py"}'),
        "metadata": {"turn_id": "turn-1"},
    }
    final_call = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed_call),
            ns(
                type="response.completed",
                response=completed_response(output=[final_call]),
            ),
        ]),
        1,
        1,
        "gpt-fake",
    )

    parsed = _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")

    assert parsed.tool_calls[0]["id"] == "call_1"
