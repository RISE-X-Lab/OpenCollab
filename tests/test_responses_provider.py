"""Contract tests for the native OpenAI Responses adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from opencollab.adapters.llm.responses_provider import (
    ResponsesEmptyOutputError,
    ResponsesProtocolError,
    _build_request_kwargs,
    _consume_stream,
    _messages_to_input,
    _parse_stream,
    complete_responses,
    parse_responses_response,
)


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
            input_tokens_details=ns(cached_tokens=80),
            cache_write_tokens=12,
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


@pytest.mark.asyncio
async def test_stream_text_uses_output_item_done_when_completed_output_is_empty():
    response = completed_response()
    stream = FakeStream([
        ns(type="response.created"),
        ns(type="response.output_text.delta", delta="OK", output_index=0),
        ns(type="response.output_item.done", output_index=0, item=message_item("OK")),
        ns(type="response.completed", response=response),
    ])

    state = await _consume_stream(stream, 1, 1)
    parsed = _parse_stream(
        state,
        [{"role": "user", "content": "Reply OK"}],
        "gpt-fake",
    )

    assert parsed.content == "OK"
    assert parsed.provider_items == [message_item("OK")]
    assert parsed.usage.input_tokens == 120
    assert parsed.usage.cache_read_tokens == 80
    assert parsed.usage.cache_creation_tokens == 12
    assert parsed.usage.reasoning_tokens == 9
    assert parsed.provider_model == "gpt-fake"
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stream_aggregates_multiple_tool_calls_and_validates_arguments():
    first = function_item("call_1", "read_file", '{"path":"a.py"}')
    second = function_item("call_2", "read_file", '{"path":"b.py"}')
    stream = FakeStream([
        ns(type="response.function_call_arguments.delta", output_index=0, delta='{"path":'),
        ns(type="response.function_call_arguments.delta", output_index=0, delta='"a.py"}'),
        ns(
            type="response.function_call_arguments.done",
            output_index=0,
            arguments='{"path":"a.py"}',
        ),
        ns(type="response.output_item.done", output_index=0, item=first),
        ns(type="response.output_item.done", output_index=1, item=second),
        ns(type="response.completed", response=completed_response()),
    ])

    state = await _consume_stream(stream, 1, 1)
    parsed = _parse_stream(state, [{"role": "user", "content": "Read both"}], "gpt-fake")

    assert [call["id"] for call in parsed.tool_calls] == ["call_1", "call_2"]
    assert parsed.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_rejects_duplicate_call_id():
    call = function_item("duplicate", "read_file", "{}")
    stream = FakeStream([
        ns(type="response.output_item.done", output_index=0, item=call),
        ns(type="response.output_item.done", output_index=1, item=call),
    ])

    with pytest.raises(ResponsesProtocolError, match="duplicate function_call"):
        await _consume_stream(stream, 1, 1)


@pytest.mark.asyncio
async def test_stream_rejects_missing_completion_and_unknown_events():
    with pytest.raises(ResponsesProtocolError, match="before response.completed"):
        await _consume_stream(FakeStream([ns(type="response.created")]), 1, 1)
    with pytest.raises(ResponsesProtocolError, match="unsupported Responses event"):
        await _consume_stream(FakeStream([ns(type="response.future_event")]), 1, 1)
    with pytest.raises(ResponsesProtocolError, match="provider rejected request"):
        await _consume_stream(
            FakeStream([
                ns(
                    type="error",
                    error={"code": "bad_request", "message": "provider rejected request"},
                )
            ]),
            1,
            1,
        )


@pytest.mark.asyncio
async def test_transient_stream_failure_reissues_without_replaying_partial_items():
    calls = 0
    first_stream_closed = False

    class TransientError(Exception):
        status_code = 502

    class BrokenStream:
        def __init__(self):
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index == 0:
                self.index += 1
                return ns(
                    type="response.output_item.done",
                    output_index=0,
                    item=function_item("abandoned", "write_file", '{"path":"old"}'),
                )
            raise TransientError("upstream stream failed")

        async def close(self):
            nonlocal first_stream_closed
            first_stream_closed = True

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return BrokenStream()
            item = message_item("recovered")
            return FakeStream([
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(
                    type="response.completed",
                    response=completed_response(output=[item]),
                ),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "work"}],
        None,
        1.0,
        1,
    )

    assert calls == 2
    assert first_stream_closed is True
    assert result.content == "recovered"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_clean_premature_eof_retries_and_discards_partial_items(monkeypatch):
    calls = 0
    first = FakeStream([
        ns(
            type="response.output_item.done",
            output_index=0,
            item=function_item("abandoned", "write_file", '{"path":"old"}'),
        )
    ])

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            item = message_item("recovered")
            return FakeStream([
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.completed", response=completed_response(output=[item])),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "work"}],
        None,
        1.0,
        1,
    )

    assert calls == 2
    assert first.closed is True
    assert result.content == "recovered"
    assert result.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_completed_empty_output_retries_entire_request(monkeypatch, stream):
    calls = 0

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                if not stream:
                    return completed_response()
                return FakeStream([
                    ns(type="response.completed", response=completed_response()),
                ])
            item = message_item("recovered")
            if not stream:
                return completed_response(output=[item])
            return FakeStream([
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.completed", response=completed_response(output=[item])),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "work"}],
        None,
        1.0,
        1,
        stream=stream,
    )

    assert calls == 2
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_completed_empty_output_exhausts_retry_budget(monkeypatch):
    calls = 0

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return FakeStream([
                ns(type="response.completed", response=completed_response()),
            ])

    with pytest.raises(ResponsesEmptyOutputError, match="no message or function call"):
        await complete_responses(
            ns(responses=Responses()),
            "gpt-fake",
            [{"role": "user", "content": "work"}],
            None,
            1.0,
            2,
        )

    assert calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,match",
    [
        (completed_response(status="failed"), "status 'failed'"),
        (completed_response(error={"message": "late failure"}), "contains error"),
        (
            completed_response(incomplete_details={"reason": "max_output_tokens"}),
            "incomplete details",
        ),
    ],
)
async def test_stream_rejects_invalid_completed_terminal(response, match):
    stream = FakeStream([
        ns(type="response.output_item.done", output_index=0, item=message_item("unsafe")),
        ns(type="response.completed", response=response),
    ])
    with pytest.raises(ResponsesProtocolError, match=match):
        await _consume_stream(stream, 1, 1, "gpt-fake")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,match",
    [
        (
            ns(
                type="response.failed",
                response=ns(error={"code": "server_error", "message": "upstream failed"}),
            ),
            "upstream failed",
        ),
        (
            ns(
                type="response.incomplete",
                response=ns(error=None, incomplete_details={"reason": "max_output_tokens"}),
            ),
            "max_output_tokens",
        ),
    ],
)
async def test_terminal_error_events_report_nested_response_details(event, match):
    with pytest.raises(ResponsesProtocolError, match=match):
        await _consume_stream(FakeStream([event]), 1, 1, "gpt-fake")


@pytest.mark.asyncio
async def test_typed_transient_error_event_retries_request(monkeypatch):
    calls = 0

    async def skip_delay(_seconds):
        return None

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", skip_delay)

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeStream([
                    ns(
                        type="error",
                        code="server_error",
                        message="temporary upstream failure",
                    )
                ])
            item = message_item("recovered")
            return FakeStream([
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.completed", response=completed_response(output=[item])),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "work"}],
        None,
        1.0,
        1,
    )

    assert calls == 2
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_stream_rejects_wrong_model_and_final_output_drift():
    wrong_model = FakeStream([
        ns(type="response.completed", response=completed_response(model="other-model")),
    ])
    with pytest.raises(ResponsesProtocolError, match="model identity mismatch"):
        await _consume_stream(wrong_model, 1, 1, "gpt-fake")

    streamed = message_item("streamed")
    final = message_item("different")
    state = await _consume_stream(
        FakeStream([
            ns(type="response.output_item.done", output_index=0, item=streamed),
            ns(
                type="response.completed",
                response=completed_response(output=[final]),
            ),
        ]),
        1,
        1,
        "gpt-fake",
    )
    with pytest.raises(ResponsesProtocolError, match="disagrees with streamed"):
        _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")


@pytest.mark.asyncio
async def test_stream_cancellation_closes_transport():
    stream = FakeStream([ns(type="response.created")], delays=[10])
    task = asyncio.create_task(_consume_stream(stream, 20, 20))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stream_rejects_bad_json_and_incomplete_argument_fragments():
    bad = function_item("bad", "read_file", "{")
    with pytest.raises(ResponsesProtocolError, match="invalid JSON"):
        await _consume_stream(
            FakeStream([ns(type="response.output_item.done", output_index=0, item=bad)]),
            1,
            1,
        )
    with pytest.raises(ResponsesProtocolError, match="incomplete tool arguments"):
        await _consume_stream(
            FakeStream([
                ns(type="response.function_call_arguments.delta", output_index=0, delta="{"),
                ns(type="response.completed", response=completed_response()),
            ]),
            1,
            1,
        )


@pytest.mark.asyncio
async def test_stream_has_distinct_first_event_and_idle_timeouts():
    with pytest.raises(ResponsesProtocolError, match="first-event timeout"):
        await _consume_stream(
            FakeStream([ns(type="response.created")], delays=[0.03]),
            0.001,
            1,
        )
    with pytest.raises(ResponsesProtocolError, match="stream-idle timeout"):
        await _consume_stream(
            FakeStream(
                [ns(type="response.created"), ns(type="response.in_progress")],
                delays=[0, 0.03],
            ),
            1,
            0.001,
        )


@pytest.mark.asyncio
async def test_first_event_timeout_includes_waiting_for_response_headers():
    create_cancelled = False

    class Responses:
        async def create(self, **_kwargs):
            nonlocal create_cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                create_cancelled = True
                raise

    with pytest.raises(ResponsesProtocolError, match="first-event timeout"):
        await complete_responses(
            ns(responses=Responses()),
            "model",
            [{"role": "user", "content": "wait"}],
            None,
            0,
            0,
            first_event_timeout=0.001,
            stream_idle_timeout=1,
            round_timeout=1,
        )

    assert create_cancelled is True


@pytest.mark.asyncio
async def test_response_header_timeout_retries_the_same_request():
    calls = 0

    class Responses:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(10)
            item = message_item("OK")
            return FakeStream([
                ns(type="response.created"),
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(type="response.completed", response=completed_response(output=[item])),
            ])

    result = await complete_responses(
        ns(responses=Responses()),
        "gpt-fake",
        [{"role": "user", "content": "wait"}],
        None,
        0,
        1,
        first_event_timeout=0.001,
        stream_idle_timeout=1,
        round_timeout=2,
    )

    assert calls == 2
    assert result.content == "OK"


@pytest.mark.asyncio
async def test_round_deadline_cancels_and_closes_stream():
    stream = FakeStream([ns(type="response.created")], delays=[10])

    class Responses:
        async def create(self, **_kwargs):
            return stream

    with pytest.raises(ResponsesProtocolError, match="round deadline exceeded"):
        await complete_responses(
            ns(responses=Responses()),
            "model",
            [{"role": "user", "content": "wait"}],
            None,
            0,
            0,
            first_event_timeout=1,
            stream_idle_timeout=1,
            round_timeout=0.001,
        )
    assert stream.closed is True


def test_item_replay_binds_function_call_output_to_original_call_id():
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque",
    }
    function = function_item("call_exact", "get_number", '{"name":"answer"}')
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "Find the number."},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_exact",
                "type": "function",
                "function": {"name": "get_number", "arguments": '{"name":"answer"}'},
            }],
            "response_items": [reasoning, function],
        },
        {"role": "tool", "tool_call_id": "call_exact", "content": "42"},
    ]

    instructions, items = _messages_to_input(messages)

    assert instructions == "Use tools."
    assert items[-1] == {
        "type": "function_call_output",
        "call_id": "call_exact",
        "output": "42",
    }
    assert reasoning in items
    assert function in items


@pytest.mark.parametrize(
    "messages,match",
    [
        (
            [{"role": "tool", "tool_call_id": "orphan", "content": "x"}],
            "no matching call_id",
        ),
        (
            [
                {
                    "role": "assistant",
                    "response_items": [
                        function_item("same", "one", "{}"),
                        function_item("same", "two", "{}"),
                    ],
                }
            ],
            "duplicate replayed call_id",
        ),
    ],
)
def test_item_replay_rejects_orphan_and_duplicate_call_ids(messages, match):
    with pytest.raises(ResponsesProtocolError, match=match):
        _messages_to_input(messages)


def test_item_replay_rejects_tool_call_identity_drift():
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call_exact",
            "type": "function",
            "function": {"name": "get_number", "arguments": '{"name":"changed"}'},
        }],
        "response_items": [
            function_item("call_exact", "get_number", '{"name":"answer"}')
        ],
    }]
    with pytest.raises(ResponsesProtocolError, match="response_items disagree"):
        _messages_to_input(messages)


def test_request_maps_instructions_tools_reasoning_and_sampling():
    kwargs = _build_request_kwargs(
        "gpt-5.6-sol",
        [
            {"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "Fix it."},
        ],
        [{
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write one file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "strict": True,
            },
        }],
        1.0,
        tool_choice={"type": "function", "function": {"name": "write_file"}},
        top_p=0.95,
        max_output_tokens=32768,
        reasoning_effort="xhigh",
    )

    assert kwargs["instructions"] == "You are a coder."
    assert kwargs["input"] == [{"role": "user", "content": "Fix it."}]
    assert kwargs["tools"][0]["name"] == "write_file"
    assert kwargs["tool_choice"] == {"type": "function", "name": "write_file"}
    assert kwargs["reasoning"] == {"effort": "xhigh"}
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["store"] is False
    assert kwargs["stream"] is True
    assert kwargs["temperature"] == 1.0
    assert kwargs["top_p"] == 0.95
    assert kwargs["max_output_tokens"] == 32768


def test_non_streaming_text_and_missing_usage_are_supported():
    response = completed_response(output=[message_item("done")])
    parsed = parse_responses_response(
        response,
        [{"role": "user", "content": "work"}],
        expected_model="gpt-fake",
    )
    assert parsed.content == "done"
    assert parsed.usage.estimated is False

    response.usage = None
    estimated = parse_responses_response(
        response,
        [{"role": "user", "content": "work"}],
        expected_model="gpt-fake",
    )
    assert estimated.usage.estimated is True
    assert estimated.usage.cache_read_tokens is None
    assert estimated.usage.reasoning_tokens is None


def test_non_streaming_rejects_wrong_model_and_estimates_invalid_usage():
    wrong_model = completed_response(output=[message_item("done")], model="other-model")
    with pytest.raises(ResponsesProtocolError, match="model identity mismatch"):
        parse_responses_response(
            wrong_model,
            [{"role": "user", "content": "work"}],
            expected_model="gpt-fake",
        )

    response = completed_response(output=[message_item("done")])
    response.usage.input_tokens = -1
    response.usage.output_tokens = 0
    response.usage.input_tokens_details.cached_tokens = -4
    parsed = parse_responses_response(
        response,
        [{"role": "user", "content": "work"}],
        expected_model="gpt-fake",
    )
    assert parsed.usage.estimated is True
    assert parsed.usage.input_tokens > 0
    assert parsed.usage.output_tokens > 0
    assert parsed.usage.cache_read_tokens is None
