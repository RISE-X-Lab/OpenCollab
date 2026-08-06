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

from opencollab.adapters.llm.responses_provider import (
    ResponsesProtocolError,
    _consume_stream,
    _parse_stream,
)


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


def _reasoning_item(encrypted_content: str | None) -> dict[str, object]:
    item: dict[str, object] = {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "checked"}],
    }
    if encrypted_content is not None:
        item["encrypted_content"] = encrypted_content
    return item


@pytest.mark.asyncio
async def test_stream_rejects_conflicting_reasoning_ciphertext():
    streamed = _reasoning_item("stream-A")
    terminal = _reasoning_item("terminal-B")
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed),
            ns(
                type="response.completed",
                response=completed_response(output=[terminal]),
            ),
        ]),
        1,
        1,
        "gpt-fake",
    )

    with pytest.raises(ResponsesProtocolError, match="disagrees with streamed"):
        _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_cipher,terminal_cipher", [("stream-A", None), (None, "terminal-A")])
async def test_stream_accepts_one_sided_reasoning_ciphertext_projection(
    stream_cipher,
    terminal_cipher,
):
    streamed = _reasoning_item(stream_cipher)
    terminal = _reasoning_item(terminal_cipher)
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed),
            ns(
                type="response.completed",
                response=completed_response(output=[terminal]),
            ),
        ]),
        1,
        1,
        "gpt-fake",
    )

    parsed = _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")

    expected_cipher = stream_cipher or terminal_cipher
    assert parsed.provider_items[0]["encrypted_content"] == expected_cipher


@pytest.mark.asyncio
async def test_stream_rejects_terminal_output_omission():
    streamed = message_item("streamed-only")
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed),
            ns(type="response.completed", response=completed_response(output=[])),
        ]),
        1,
        1,
        "gpt-fake",
    )

    with pytest.raises(ResponsesProtocolError, match="disagrees with streamed"):
        _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")
