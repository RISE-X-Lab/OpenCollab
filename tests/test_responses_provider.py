"""Contract tests for the native OpenAI Responses adapter."""

from __future__ import annotations

import asyncio

import pytest
from responses_provider_test_support import (
    FakeStream,
    completed_response,
    function_item,
    message_item,
    ns,
)

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


@pytest.mark.asyncio
async def test_stream_text_requires_matching_completed_output():
    item = message_item("OK")
    response = completed_response(output=[item])
    stream = FakeStream(
        [
            ns(type="response.created"),
            ns(type="response.output_text.delta", delta="OK", output_index=0),
            ns(type="response.output_item.done", output_index=0, item=item),
            ns(type="response.completed", response=response),
        ]
    )

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
    stream = FakeStream(
        [
            ns(type="response.function_call_arguments.delta", output_index=0, delta='{"path":'),
            ns(type="response.function_call_arguments.delta", output_index=0, delta='"a.py"}'),
            ns(
                type="response.function_call_arguments.done",
                output_index=0,
                arguments='{"path":"a.py"}',
            ),
            ns(type="response.output_item.done", output_index=0, item=first),
            ns(type="response.output_item.done", output_index=1, item=second),
            ns(type="response.completed", response=completed_response(output=[first, second])),
        ]
    )

    state = await _consume_stream(stream, 1, 1)
    parsed = _parse_stream(state, [{"role": "user", "content": "Read both"}], "gpt-fake")

    assert [call["id"] for call in parsed.tool_calls] == ["call_1", "call_2"]
    assert parsed.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_rejects_duplicate_call_id():
    call = function_item("duplicate", "read_file", "{}")
    stream = FakeStream(
        [
            ns(type="response.output_item.done", output_index=0, item=call),
            ns(type="response.output_item.done", output_index=1, item=call),
        ]
    )

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
            FakeStream(
                [
                    ns(
                        type="error",
                        error={"code": "bad_request", "message": "provider rejected request"},
                    )
                ]
            ),
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
            return FakeStream(
                [
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(
                        type="response.completed",
                        response=completed_response(output=[item]),
                    ),
                ]
            )

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
    first = FakeStream(
        [
            ns(
                type="response.output_item.done",
                output_index=0,
                item=function_item("abandoned", "write_file", '{"path":"old"}'),
            )
        ]
    )

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
            return FakeStream(
                [
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(type="response.completed", response=completed_response(output=[item])),
                ]
            )

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
                return FakeStream(
                    [
                        ns(type="response.completed", response=completed_response()),
                    ]
                )
            item = message_item("recovered")
            if not stream:
                return completed_response(output=[item])
            return FakeStream(
                [
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(type="response.completed", response=completed_response(output=[item])),
                ]
            )

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
            return FakeStream(
                [
                    ns(type="response.completed", response=completed_response()),
                ]
            )

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
    stream = FakeStream(
        [
            ns(type="response.output_item.done", output_index=0, item=message_item("unsafe")),
            ns(type="response.completed", response=response),
        ]
    )
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
                return FakeStream(
                    [
                        ns(
                            type="error",
                            code="server_error",
                            message="temporary upstream failure",
                        )
                    ]
                )
            item = message_item("recovered")
            return FakeStream(
                [
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(type="response.completed", response=completed_response(output=[item])),
                ]
            )

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
async def test_stream_accepts_provider_resolved_model_alias():
    item = message_item("done")
    state = await _consume_stream(
        FakeStream(
            [
                ns(type="response.output_item.done", output_index=0, item=item),
                ns(
                    type="response.completed",
                    response=completed_response(output=[item], model="other-model"),
                ),
            ]
        ),
        1,
        1,
        "gpt-fake",
    )

    parsed = _parse_stream(state, [{"role": "user", "content": "work"}], "gpt-fake")

    assert parsed.provider_model == "other-model"


@pytest.mark.asyncio
async def test_stream_rejects_final_output_drift():
    streamed = message_item("streamed")
    final = message_item("different")
    state = await _consume_stream(
        FakeStream(
            [
                ns(type="response.output_item.done", output_index=0, item=streamed),
                ns(
                    type="response.completed",
                    response=completed_response(output=[final]),
                ),
            ]
        ),
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
            FakeStream(
                [
                    ns(type="response.function_call_arguments.delta", output_index=0, delta="{"),
                    ns(type="response.completed", response=completed_response()),
                ]
            ),
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
            return FakeStream(
                [
                    ns(type="response.created"),
                    ns(type="response.output_item.done", output_index=0, item=item),
                    ns(type="response.completed", response=completed_response(output=[item])),
                ]
            )

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
            "tool_calls": [
                {
                    "id": "call_exact",
                    "type": "function",
                    "function": {"name": "get_number", "arguments": '{"name":"answer"}'},
                }
            ],
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


def test_responses_preserves_image_url_content_blocks():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png", "detail": "high"}},
        ],
    }]
    _, items = _messages_to_input(messages)
    assert items == [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "What is shown?"},
            {"type": "input_image", "image_url": "https://example.test/a.png", "detail": "high"},
        ],
    }]


def test_responses_rejects_tools_for_models_without_function_calling():
    with pytest.raises(ResponsesProtocolError, match="does not support function tools"):
        _build_request_kwargs(
            "o1-mini",
            [{"role": "user", "content": "work"}],
            [{"type": "function", "function": {"name": "f", "parameters": {}}}],
            0.2,
        )


@pytest.mark.parametrize("model", ["o1-pro", "vendor/o1-pro-2025-04-01", "o3-mini", "gateway/o3-mini-2026-01-01"])
def test_responses_omits_unsupported_sampling(model):
    kwargs = _build_request_kwargs(model, [{"role": "user", "content": "work"}], None, 0.2, top_p=0.9)
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert kwargs["stream"] is ("o1-pro" not in model)


def test_responses_omits_reasoning_for_non_reasoning_models():
    kwargs = _build_request_kwargs(
        "gpt-4o", [{"role": "user", "content": "work"}], None, 0.2, reasoning_effort="low"
    )
    assert "reasoning" not in kwargs


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
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_exact",
                    "type": "function",
                    "function": {"name": "get_number", "arguments": '{"name":"changed"}'},
                }
            ],
            "response_items": [function_item("call_exact", "get_number", '{"name":"answer"}')],
        }
    ]
    with pytest.raises(ResponsesProtocolError, match="response_items disagree"):
        _messages_to_input(messages)


def test_prompt_cache_key_is_stable_within_one_run_and_isolated_between_runs():
    messages = [
        {"role": "system", "content": "You are a coder."},
        {"role": "user", "content": "Fix it."},
    ]
    first = _build_request_kwargs(
        "gpt-5.6-luna",
        messages,
        None,
        1.0,
        prompt_cache_namespace="client-a",
        response_session_id="session-a",
    )
    next_turn = _build_request_kwargs(
        "gpt-5.6-luna",
        [*messages, {"role": "assistant", "content": "Inspecting."}],
        None,
        1.0,
        prompt_cache_namespace="client-a",
        response_session_id="session-a",
    )
    other_run = _build_request_kwargs(
        "gpt-5.6-luna",
        messages,
        None,
        1.0,
        prompt_cache_namespace="client-a",
        response_session_id="session-b",
    )
    other_client = _build_request_kwargs(
        "gpt-5.6-luna",
        messages,
        None,
        1.0,
        prompt_cache_namespace="client-b",
        response_session_id="session-a",
    )

    assert first["prompt_cache_key"] == next_turn["prompt_cache_key"]
    assert first["prompt_cache_key"] != other_run["prompt_cache_key"]
    assert first["prompt_cache_key"] != other_client["prompt_cache_key"]
    assert len(first["prompt_cache_key"]) == 64


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


def test_non_streaming_usage_accepts_top_level_cache_write_fallback():
    response = completed_response(output=[message_item("done")])
    del response.usage.input_tokens_details.cache_write_tokens
    response.usage.cache_write_tokens = 4

    parsed = parse_responses_response(
        response,
        [{"role": "user", "content": "work"}],
        expected_model="gpt-fake",
    )

    assert parsed.usage.cache_creation_tokens == 4


def test_non_streaming_requires_provider_model_identity():
    response = completed_response(output=[message_item("done")], model="")

    with pytest.raises(ResponsesProtocolError, match="missing model identity"):
        parse_responses_response(
            response,
            [{"role": "user", "content": "work"}],
            expected_model="gpt-fake",
        )


def test_non_streaming_records_provider_resolved_model_alias_and_estimates_invalid_usage():
    aliased = completed_response(output=[message_item("done")], model="other-model")
    parsed_alias = parse_responses_response(
        aliased,
        [{"role": "user", "content": "work"}],
        expected_model="gpt-fake",
    )
    assert parsed_alias.provider_model == "other-model"

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
