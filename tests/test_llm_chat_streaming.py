"""Contract tests for the opt-in chat-completions streaming path.

Streaming exists for one reason: several OpenAI-compatible endpoints return the
model's reasoning text ONLY over the streamed wire format. Reassembling a
completion from chunks re-implements everything the single-response parser did
for free, so most of these tests pin a failure mode that would otherwise be
silent — a truncated turn read as a complete one, one tool call's arguments
spliced onto another's, a budget driven by estimated tokens.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.errors import (
    StreamedUsageUnavailableError,
    TransientProviderError,
)
from opencollab.adapters.llm.openai_provider import (
    _build_request_kwargs,
    _parse_response,
    complete_openai,
)
from opencollab.adapters.llm.retry import is_retryable_error

MODEL = "deepseek-fake"
USAGE = {
    "prompt_tokens": 96,
    "completion_tokens": 15,
    "total_tokens": 111,
    "completion_tokens_details": {"reasoning_tokens": 13},
    "prompt_tokens_details": {"cached_tokens": 7},
}
MESSAGES = [{"role": "user", "content": "What is 17*23?"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_population",
            "description": "Population of a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    },
]


# ---------------------------------------------------------------------------
# Fixtures: real SDK chunk objects, a fake stream, a fake client
# ---------------------------------------------------------------------------


def chunk(
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    model: str = MODEL,
    index: int = 0,
    no_choices: bool = False,
) -> ChatCompletionChunk:
    """A genuine ``ChatCompletionChunk``.

    Validated through the SDK model rather than faked with ``SimpleNamespace``
    so ``reasoning_content`` really has to survive as a pydantic extra, which is
    the only reason the provider can read it at all.
    """
    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [],
    }
    if not no_choices:
        payload["choices"] = [
            {"index": index, "delta": delta or {}, "finish_reason": finish_reason}
        ]
    if usage is not None:
        payload["usage"] = usage
    return ChatCompletionChunk.model_validate(payload)


def usage_chunk(usage: dict[str, Any] | None = None) -> ChatCompletionChunk:
    """The terminal chunk the endpoint sends: usage, and an EMPTY choices list."""
    return chunk(no_choices=True, usage=usage if usage is not None else USAGE)


def text_script(
    text: str = "391",
    reasoning: str | None = None,
    *,
    finish_reason: str = "stop",
    with_usage: bool = True,
) -> list[ChatCompletionChunk]:
    chunks = [chunk(delta={"role": "assistant"})]
    if reasoning:
        chunks += [chunk(delta={"reasoning_content": piece}) for piece in reasoning]
    chunks += [chunk(delta={"content": piece}) for piece in text]
    chunks.append(chunk(delta={}, finish_reason=finish_reason))
    if with_usage:
        chunks.append(usage_chunk())
    return chunks


class FakeStream:
    def __init__(self, chunks: list[Any], delays: list[float] | None = None):
        self._chunks = iter(chunks)
        self._delays = iter(delays or [0.0] * len(chunks))
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        delay = next(self._delays, 0.0)
        if delay:
            await asyncio.sleep(delay)
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self):
        self.closed = True


class FakeClient:
    """Stands in for ``AsyncOpenAI``; records every create() kwargs dict."""

    def __init__(self, scripts: list[Any], delays: list[list[float]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self._scripts = list(scripts)
        self._delays = list(delays or [])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        if isinstance(script, ChatCompletion):
            return script
        delays = self._delays.pop(0) if self._delays else None
        stream = FakeStream(script, delays)
        self.streams.append(stream)
        return stream


async def run_stream(
    script: list[Any],
    *,
    tools: list[dict] | None = None,
    messages: list[dict] | None = None,
    max_retries: int = 0,
    **kwargs: Any,
):
    client = FakeClient([script])
    response = await complete_openai(
        client,
        MODEL,
        messages or MESSAGES,
        tools,
        0.0,
        max_retries,
        stream=True,
        **kwargs,
    )
    return response, client


def tool_delta(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    call: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        call["id"] = call_id
        call["type"] = "function"
    return {"tool_calls": [call]}


# ---------------------------------------------------------------------------
# Equivalence with the runs already on disk
# ---------------------------------------------------------------------------


async def test_stream_off_sends_no_stream_keys_to_create():
    """The switch off must not add a single request key: the existing runs are
    the control arm, and a changed body makes them incomparable."""
    completion = ChatCompletion.model_validate({
        "id": "chatcmpl-1",
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
    })
    client = FakeClient([completion])

    await complete_openai(client, MODEL, MESSAGES, None, 0.0, 0)

    assert "stream" not in client.calls[0]
    assert "stream_options" not in client.calls[0]


async def test_stream_on_adds_stream_and_include_usage():
    _, client = await run_stream(text_script())

    assert client.calls[0]["stream"] is True
    assert client.calls[0]["stream_options"] == {"include_usage": True}


async def test_streamed_and_non_streamed_answers_parse_identically():
    """One logical answer, delivered both ways, must produce the same
    ``LLMResponse`` — the single guard against the two parsers drifting."""
    completion = ChatCompletion.model_validate({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "391",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": USAGE,
    })
    kwargs = _build_request_kwargs(MODEL, MESSAGES, TOOLS, 0.0)
    expected = _parse_response(completion, kwargs["messages"], kwargs["tools"])

    script = [
        chunk(delta={"role": "assistant"}),
        chunk(delta={"content": "3"}),
        chunk(delta={"content": "91"}),
        chunk(delta=tool_delta(0, call_id="call_1", name="get_weather", arguments='{"city": ')),
        chunk(delta=tool_delta(0, arguments='"Paris"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]
    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed == expected


# ---------------------------------------------------------------------------
# finish_reason: the one field whose loss is invisible
# ---------------------------------------------------------------------------


async def test_finish_reason_length_survives_streaming():
    """A turn cut off at max_tokens must still report ``length`` — the session's
    truncation guard reads nothing else."""
    streamed, _ = await run_stream(text_script(finish_reason="length"))

    assert streamed.finish_reason == "length"


async def test_finish_reason_is_read_from_a_non_final_chunk():
    """The configured endpoint puts finish_reason on the second-to-last chunk,
    ahead of the usage chunk; reading only the last chunk would lose it."""
    script = text_script()
    assert script[-1].choices == []

    streamed, _ = await run_stream(script)

    assert streamed.finish_reason == "stop"


async def test_stream_without_any_finish_reason_raises():
    """No finish_reason means the stream broke. Filling in a default would let a
    truncated answer — possibly a half-written tool call — be executed as if
    complete, with nothing in the trajectory saying so."""
    script = [chunk(delta={"content": "39"}), chunk(delta={"content": "1"}), usage_chunk()]

    with pytest.raises(TransientProviderError, match="finish_reason"):
        await run_stream(script)


async def test_stream_interrupted_mid_tool_call_raises():
    script = [
        chunk(delta=tool_delta(0, call_id="call_1", name="get_weather", arguments='{"ci')),
    ]

    with pytest.raises(TransientProviderError, match="finish_reason"):
        await run_stream(script, tools=TOOLS)


async def test_empty_stream_raises():
    with pytest.raises(TransientProviderError, match="no chunks"):
        await run_stream([])


async def test_interrupted_stream_is_retried_as_one_unit():
    """create() returns at the response headers, so a retry wrapped around it
    alone would never re-run a mid-stream break."""
    client = FakeClient([[chunk(delta={"content": "39"})], text_script()])

    response = await complete_openai(
        client, MODEL, MESSAGES, None, 0.0, 1, stream=True
    )

    assert response.content == "391"
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# usage / budget
# ---------------------------------------------------------------------------


async def test_usage_is_read_from_the_final_empty_choices_chunk():
    streamed, _ = await run_stream(text_script())

    assert streamed.usage.input_tokens == 96
    assert streamed.usage.output_tokens == 15
    assert streamed.usage.estimated is False


async def test_usage_detail_fields_match_the_non_streaming_parse():
    streamed, _ = await run_stream(text_script())

    assert streamed.usage.cache_read_tokens == 7
    assert streamed.usage.reasoning_tokens == 13


async def test_missing_usage_chunk_is_raised_not_silently_estimated():
    """Without a usage block the budget meter, the USD ledger and every token
    comparison would run on an estimate flagged by a boolean nobody reads."""
    with pytest.raises(StreamedUsageUnavailableError):
        await run_stream(text_script(with_usage=False))


async def test_missing_usage_is_not_retried():
    """The stream completed, so repeating it buys the same answer at full price."""
    error = StreamedUsageUnavailableError("chat completion stream reported no usable usage block")

    assert is_retryable_error(error) is False


# ---------------------------------------------------------------------------
# tool calls
# ---------------------------------------------------------------------------


async def test_tool_arguments_join_across_chunks():
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments="")),
        chunk(delta=tool_delta(0, arguments='{"city"')),
        chunk(delta=tool_delta(0, arguments=': "Par')),
        chunk(delta=tool_delta(0, arguments='is"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.tool_calls[0]["function"]["arguments"] == '{"city": "Paris"}'


async def test_parallel_tool_calls_keep_separate_index_slots():
    """Interleaved fragments must follow ``index``, not arrival order: appending
    in arrival order can splice two calls into one that still parses."""
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments='{"city": ')),
        chunk(delta=tool_delta(1, call_id="c2", name="get_population", arguments='{"city": ')),
        chunk(delta=tool_delta(0, arguments='"Paris"}')),
        chunk(delta=tool_delta(1, arguments='"Berlin"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert [call["id"] for call in streamed.tool_calls] == ["c1", "c2"]
    assert streamed.tool_calls[0]["function"] == {
        "name": "get_weather",
        "arguments": '{"city": "Paris"}',
    }
    assert streamed.tool_calls[1]["function"] == {
        "name": "get_population",
        "arguments": '{"city": "Berlin"}',
    }


async def test_tool_id_and_name_survive_blank_later_fragments():
    """The endpoint repeats ``name`` as "" and ``id`` as null after the first
    fragment; assigning instead of keeping would erase both."""
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments="")),
        chunk(delta=tool_delta(0, name="", arguments='{"city": "Paris"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.tool_calls[0]["id"] == "c1"
    assert streamed.tool_calls[0]["function"]["name"] == "get_weather"


async def test_fragmented_tool_name_is_assembled():
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_")),
        chunk(delta=tool_delta(0, name="weather", arguments='{"city": "Paris"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.tool_calls[0]["function"]["name"] == "get_weather"


async def test_assembled_tool_name_outside_the_request_tools_raises():
    """A misassembled name would otherwise call a different tool, or none, with
    no signal at all."""
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="weather", arguments="{}")),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    with pytest.raises(TransientProviderError, match="unregistered name"):
        await run_stream(script, tools=TOOLS)


async def test_tool_call_without_an_id_raises():
    script = [
        chunk(delta=tool_delta(0, name="get_weather", arguments="{}")),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    with pytest.raises(TransientProviderError, match="never carried an id"):
        await run_stream(script, tools=TOOLS)


async def test_assembled_arguments_that_are_not_json_raise():
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments='{"city"')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    with pytest.raises(TransientProviderError, match="invalid JSON"):
        await run_stream(script, tools=TOOLS)


async def test_stream_applies_the_shared_empty_object_prefix_repair():
    """``{}{...}`` is repaired on the streamed path exactly as on the other."""
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments="{}{")),
        chunk(delta=tool_delta(0, arguments='"city": "Paris"}')),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.tool_calls[0]["function"]["arguments"] == '{"city": "Paris"}'


async def test_tool_fragment_without_an_index_raises():
    delta = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                index=None,
                id="c1",
                function=SimpleNamespace(name="get_weather", arguments="{}"),
            )
        ],
    )
    fake_chunk = SimpleNamespace(
        model=MODEL,
        usage=None,
        choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)],
    )

    with pytest.raises(TransientProviderError, match="no index"):
        await run_stream([fake_chunk], tools=TOOLS)


# ---------------------------------------------------------------------------
# reasoning — the reason this path exists
# ---------------------------------------------------------------------------


async def test_reasoning_content_deltas_concatenate_in_order():
    streamed, _ = await run_stream(text_script("391", reasoning="17*23=391"))

    assert streamed.reasoning == "17*23=391"


async def test_reasoning_alias_field_is_accepted():
    """OpenRouter-style gateways spell it ``reasoning``; without this the text
    would silently stay empty and look exactly like today."""
    script = [
        chunk(delta={"reasoning": "thinking "}),
        chunk(delta={"reasoning": "hard"}),
        chunk(delta={"content": "391"}),
        chunk(delta={}, finish_reason="stop"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script)

    assert streamed.reasoning == "thinking hard"


async def test_only_the_first_reasoning_spelling_is_read():
    """A gateway sending both keys must not duplicate the chain of thought —
    duplicated reasoning is unreadable as a bug to a human."""
    script = [
        chunk(delta={"reasoning_content": "step one "}),
        chunk(delta={"reasoning_content": "step two", "reasoning": "step two"}),
        chunk(delta={"content": "391"}),
        chunk(delta={}, finish_reason="stop"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script)

    assert streamed.reasoning == "step one step two"


async def test_the_reasoning_spelling_stays_locked_for_the_whole_turn():
    """Once a spelling has produced text, the other one is ignored even on a
    later chunk that carries only the alias — otherwise a gateway echoing the
    same thought under both names concatenates it twice."""
    script = [
        chunk(delta={"reasoning_content": "step one"}),
        chunk(delta={"reasoning": "step one"}),
        chunk(delta={"content": "391"}),
        chunk(delta={}, finish_reason="stop"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script)

    assert streamed.reasoning == "step one"


async def test_streaming_does_not_send_recorded_reasoning_back():
    """Recording chain-of-thought must not turn into resending it: that inflates
    input tokens and some endpoints reject it outright."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "reasoning_content": "private thought"},
    ]

    _, client = await run_stream(text_script(), messages=history)

    assert all("reasoning_content" not in m for m in client.calls[0]["messages"])


async def test_non_streaming_still_sends_recorded_reasoning_back():
    """The off path keeps today's behaviour, including this one."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "reasoning_content": "private thought"},
    ]

    kwargs = _build_request_kwargs(MODEL, history, None, 0.0)

    assert kwargs["messages"][1]["reasoning_content"] == "private thought"


async def test_reasoning_only_turn_is_rescued_into_content():
    """A turn with thinking but no answer and no tool call must not read as an
    empty stop — the shared rescue rung applies here too."""
    script = [
        chunk(delta={"reasoning_content": "I considered it."}),
        chunk(delta={}, finish_reason="stop"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script)

    assert streamed.content == "I considered it."
    assert streamed.reasoning == "I considered it."


async def test_kimi_markup_in_streamed_content_is_recovered():
    """The special-token recovery has to run on the ASSEMBLED text, not on
    fragments; losing it would turn an intended tool call into prose."""
    markup = (
        "<|tool_calls_section_begin|><|tool_call_begin|>functions.get_weather:c9"
        '<|tool_call_argument_begin|>{"city": "Paris"}<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    script = [chunk(delta={"content": piece}) for piece in (markup[:40], markup[40:])]
    script += [chunk(delta={}, finish_reason="stop"), usage_chunk()]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.tool_calls[0]["function"]["name"] == "get_weather"
    assert streamed.usage.markup_recovered == 1


async def test_provider_model_comes_from_the_first_chunk():
    """Later frames may omit the model; the first one that carries it wins, so
    ``provider_model`` never degrades to None on the streamed path."""
    script = text_script()
    script.append(
        SimpleNamespace(model=None, usage=None, choices=[])
    )

    streamed, _ = await run_stream(script)

    assert streamed.provider_model == MODEL


async def test_content_absent_stays_none_not_empty_string():
    """The trajectory stores this verbatim; '""' and 'null' are not the same
    value to a script reading old and new runs together."""
    script = [
        chunk(delta=tool_delta(0, call_id="c1", name="get_weather", arguments="{}")),
        chunk(delta={}, finish_reason="tool_calls"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script, tools=TOOLS)

    assert streamed.content is None


async def test_second_choice_of_a_multi_choice_stream_is_ignored():
    script = [
        chunk(delta={"content": "391"}),
        chunk(delta={"content": "IGNORED"}, index=1),
        chunk(delta={}, finish_reason="stop"),
        usage_chunk(),
    ]

    streamed, _ = await run_stream(script)

    assert streamed.content == "391"


# ---------------------------------------------------------------------------
# the two stream clocks
# ---------------------------------------------------------------------------


async def test_first_chunk_timeout_bounds_the_wait_for_the_first_chunk():
    """The elapsed time, not just the error text, decides which clock ran: a
    first chunk billed against the (much longer) idle budget would still report
    "first-chunk" while hanging for the wrong length of time."""
    client = FakeClient([text_script()], delays=[[20.0]])
    started = time.monotonic()

    with pytest.raises(TransientProviderError, match="first-chunk"):
        await complete_openai(
            client,
            MODEL,
            MESSAGES,
            None,
            0.0,
            0,
            stream=True,
            first_event_timeout=0.05,
            stream_idle_timeout=20.0,
        )

    assert time.monotonic() - started < 5.0


async def test_idle_timeout_bounds_the_gap_between_chunks():
    """A stream that stalls after its first chunk must not hang until the
    per-call wall clock: the httpx timeout only bounds one socket read."""
    client = FakeClient([text_script()], delays=[[0.0, 20.0]])
    started = time.monotonic()

    with pytest.raises(TransientProviderError, match="stream-idle"):
        await complete_openai(
            client,
            MODEL,
            MESSAGES,
            None,
            0.0,
            0,
            stream=True,
            first_event_timeout=20.0,
            stream_idle_timeout=0.05,
        )

    assert time.monotonic() - started < 5.0


async def test_stream_is_closed_even_when_consumption_fails():
    client = FakeClient([[chunk(delta={"content": "39"})]])

    with pytest.raises(TransientProviderError):
        await complete_openai(client, MODEL, MESSAGES, None, 0.0, 0, stream=True)

    assert client.streams[0].closed is True


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


# ---------------------------------------------------------------------------
# Wiring: the switch has to reach the provider from a config file
# ---------------------------------------------------------------------------


def test_env_flag_reaches_the_config(monkeypatch, tmp_path):
    from opencollab.bootstrap.config import build_config

    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "true")

    assert build_config(str(tmp_path)).llm_stream_chat is True


def test_config_defaults_the_switch_off(monkeypatch, tmp_path):
    from opencollab.bootstrap.config import build_config

    monkeypatch.delenv("OPENCOLLAB_LLM_STREAM_CHAT", raising=False)

    assert build_config(str(tmp_path)).llm_stream_chat is False


def test_agent_flag_reaches_the_llm_client(monkeypatch):
    """Without this the setting would look enabled and change nothing — the
    failure mode that costs a whole batch of runs."""
    from opencollab.adapters.llm import client as client_module
    from opencollab.bootstrap import container
    from opencollab.domain.agent import Agent

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", lambda **_kwargs: object())
    agent = Agent(name="analyst", system_prompt="hi", llm_stream_chat=True)

    resolved = container._resolve_llm(agent, None, 600.0, None)

    assert resolved.stream_chat is True
    assert container._resolve_llm(
        Agent(name="analyst", system_prompt="hi"), None, 600.0, None
    ).stream_chat is False
