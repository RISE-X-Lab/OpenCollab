"""Every provider call records when its first token arrived, or why it cannot.

``latency_s`` is the wall clock of a whole completion. It cannot separate "the
gateway queued us" from "the model generated a lot of tokens", so no rule built
on it can say which of the two a slow call was — two attempts at exactly that
inference failed their positive controls on 2026-09-07. Time to first token
splits the interval: ``ttft_s`` is transport plus prefill, and
``latency_s - ttft_s`` is decode.

TTFT exists only where the response is consumed as a stream. A non-streaming
call returns once, at the end, and has no first-token event to time. These
tests pin both halves of that: the streamed paths measure it, the
non-streaming paths say ``ttft_measured: false`` and name the reason instead of
substituting a number that would read as "the first token arrived instantly".

The cross-arm test is the point of the change. The block is written by the
shared provider layer and copied by the one trajectory writer, so it must be on
the ``llm_call`` record of every seat of every arm; the parametrization is the
arm registry's own ``role_tool_names`` table, seat by seat.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from session_run_loop_test_support import FakeAgent, FakeTracer, build_runner, run

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.first_token import TRANSPORT_TIMING_KEYS, FirstTokenTimer
from opencollab.domain.session import SessionState

MODEL = "deepseek-fake"
MESSAGES = [{"role": "user", "content": "What is 17*23?"}]
USAGE = {
    "prompt_tokens": 96,
    "completion_tokens": 15,
    "total_tokens": 111,
}

# Long enough that a clock with millisecond resolution cannot report it as
# zero, short enough that the whole file stays under a second.
FIRST_CHUNK_DELAY = 0.05
TAIL_CHUNK_DELAY = 0.05


# ---------------------------------------------------------------------------
# Fakes: a chat stream with a measurable gap before its first chunk
# ---------------------------------------------------------------------------


def _chunk(**payload: Any) -> ChatCompletionChunk:
    base = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }
    base.update(payload)
    return ChatCompletionChunk.model_validate(base)


def _chat_script() -> list[ChatCompletionChunk]:
    return [
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "done"}, "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        _chunk(choices=[], usage=USAGE),
    ]


def _completion() -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": USAGE,
        }
    )


class _FakeStream:
    """Sleeps before the first chunk, and again before the last one.

    Both gaps matter: the first is what TTFT must see, the second is what makes
    the call's total latency strictly larger than its TTFT, so a test can tell
    a real measurement from ``ttft_s = latency_s``.
    """

    def __init__(self, chunks: list[Any], delays: list[float]):
        self._chunks = iter(chunks)
        self._delays = iter(delays)
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


class _FakeChatClient:
    """Stands in for ``AsyncOpenAI`` on the chat-completions path."""

    def __init__(self, *, stream: bool):
        self._stream = stream
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._stream:
            await asyncio.sleep(FIRST_CHUNK_DELAY + TAIL_CHUNK_DELAY)
            return _completion()
        script = _chat_script()
        delays = [FIRST_CHUNK_DELAY] + [0.0] * (len(script) - 2) + [TAIL_CHUNK_DELAY]
        return _FakeStream(script, delays)


def _assert_block_shape(block: Any) -> dict[str, Any]:
    assert isinstance(block, dict), f"expected a transport-timing dict, got {block!r}"
    assert set(block) == set(TRANSPORT_TIMING_KEYS), (
        f"keys drifted: {sorted(set(block) ^ set(TRANSPORT_TIMING_KEYS))}"
    )
    return block


# ---------------------------------------------------------------------------
# Which paths can measure TTFT at all
# ---------------------------------------------------------------------------
#
# Every case below goes through ``LLMClient.complete`` rather than a provider
# function, because that method is what an arm actually calls and what installs
# the timer. A test that called the provider directly would pass while the
# production path recorded nothing.


def _chat_client(*, stream: bool) -> tuple[LLMClient, _FakeChatClient]:
    fake = _FakeChatClient(stream=stream)
    client = LLMClient(
        model=MODEL,
        api_key="unused",
        base_url="http://fake.invalid/v1",
        stream_chat=stream,
    )
    client._openai = fake
    return client, fake


def test_streamed_chat_completion_measures_time_to_first_token():
    client, _fake = _chat_client(stream=True)
    response = run(client.complete(MESSAGES))

    block = _assert_block_shape(response.transport_timing)
    assert block["ttft_measured"] is True
    assert block["ttft_unavailable_reason"] is None
    assert block["ttft_source"] == "chat_stream_first_chunk"
    assert block["streamed"] is True
    assert block["ttft_s"] >= FIRST_CHUNK_DELAY
    # The tail gap must land outside TTFT, or the number is just the latency
    # under a new name.
    assert block["ttft_s"] < block["attempt_s"]
    assert block["first_token_at"] > block["request_started_at"]
    assert block["attempts"] == 1


def test_non_streamed_chat_completion_reports_ttft_as_unmeasured():
    client, _fake = _chat_client(stream=False)
    response = run(client.complete(MESSAGES))

    block = _assert_block_shape(response.transport_timing)
    assert block["ttft_measured"] is False
    # Not zero: "the first token arrived instantly" and "nobody watched for it"
    # must not read the same downstream.
    assert block["ttft_s"] is None
    assert block["ttft_unavailable_reason"] == "response_not_streamed"
    assert block["ttft_source"] is None
    assert block["streamed"] is False
    assert block["request_started_at"] is not None
    assert block["first_token_at"] is None
    assert block["attempt_s"] >= FIRST_CHUNK_DELAY


def test_anthropic_completion_reports_ttft_as_unmeasured():
    class _Messages:
        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                stop_reason="end_turn",
                model="claude-fake",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )

    client = LLMClient(model="claude-fake", provider="anthropic", api_key="unused")
    client._anthropic = SimpleNamespace(messages=_Messages())
    response = run(client.complete(MESSAGES))

    block = _assert_block_shape(response.transport_timing)
    assert block["ttft_measured"] is False
    assert block["ttft_s"] is None
    assert block["ttft_unavailable_reason"] == "response_not_streamed"
    assert block["streamed"] is False


def _responses_output() -> list[dict[str, Any]]:
    return [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
    ]


def _completed_responses_object(model: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_1",
        status="completed",
        error=None,
        incomplete_details=None,
        model=model,
        output=_responses_output(),
        usage=SimpleNamespace(
            input_tokens=10,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens=2,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            total_tokens=12,
        ),
    )


def _responses_client(model: str, create) -> LLMClient:
    client = LLMClient(
        model=model,
        api_key="unused",
        base_url="http://fake.invalid/v1",
        wire_protocol="responses",
    )
    client._openai = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client


def test_streamed_responses_call_measures_time_to_first_token():
    model = "gpt-5-fake"
    events = [
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=_responses_output()[0],
        ),
        SimpleNamespace(
            type="response.completed",
            response=_completed_responses_object(model),
        ),
    ]

    async def create(**_kwargs: Any) -> Any:
        return _FakeStream(list(events), [FIRST_CHUNK_DELAY, TAIL_CHUNK_DELAY])

    response = run(_responses_client(model, create).complete(MESSAGES))

    block = _assert_block_shape(response.transport_timing)
    assert block["ttft_measured"] is True
    assert block["ttft_source"] == "responses_stream_first_event"
    assert block["streamed"] is True
    assert block["ttft_s"] >= FIRST_CHUNK_DELAY
    assert block["ttft_s"] < block["attempt_s"]


def test_responses_model_that_cannot_stream_reports_ttft_as_unmeasured():
    """``o1-pro`` declares ``supports_responses_streaming=False``, so even on the
    streaming wire protocol this model's calls have no first-token event."""
    model = "o1-pro"

    async def create(**_kwargs: Any) -> Any:
        await asyncio.sleep(FIRST_CHUNK_DELAY)
        return _completed_responses_object(model)

    response = run(_responses_client(model, create).complete(MESSAGES))

    block = _assert_block_shape(response.transport_timing)
    assert block["ttft_measured"] is False
    assert block["ttft_s"] is None
    assert block["ttft_unavailable_reason"] == "response_not_streamed"
    assert block["streamed"] is False


# ---------------------------------------------------------------------------
# The cross-arm contract
# ---------------------------------------------------------------------------

_SINGLE_BUNDLE = ("apply_patch", "bash", "file_read", "file_write", "grep", "run_tests", "submit")
_TEAM_WORKING_BUNDLE = (
    "apply_patch",
    "bash",
    "file_read",
    "file_write",
    "grep",
    "message_agent",
    "run_tests",
    "submit",
    "team_status",
)
_TEAM_TESTER_BUNDLE = (
    "bash",
    "file_read",
    "git_diff",
    "grep",
    "message_agent",
    "run_tests",
    "submit",
    "team_status",
)
_SCRIPTED_TESTER_BUNDLE = ("bash", "file_read", "git_diff", "grep", "run_tests", "submit")
_READING_ANALYST_BUNDLE = ("bash", "file_read", "grep", "run_tests", "submit")

#: Every seat of every arm the batch driver runs, copied from the alignment
#: registry's ``role_tool_names`` factor (OpenCollab-Eval
#: ``experiment/arm_registry.py``). A seat this list omits is a seat this
#: contract does not cover, so it is enumerated rather than sampled.
ARM_SEATS: tuple[tuple[str, str, tuple[str, ...], int], ...] = (
    ("single", "swe_agent", _SINGLE_BUNDLE, 0),
    ("best-of-n", "swe_agent", _SINGLE_BUNDLE, 0),
    ("team", "analyst", _TEAM_WORKING_BUNDLE, 0),
    ("team", "coder", _TEAM_WORKING_BUNDLE, 1),
    ("team", "tester", _TEAM_TESTER_BUNDLE, 2),
    ("self-collaboration", "analyst", _SINGLE_BUNDLE, -1),
    ("self-collaboration", "coder:r1", _SINGLE_BUNDLE, -1),
    ("self-collaboration", "tester:r1", _SCRIPTED_TESTER_BUNDLE, -1),
    ("self-collaboration", "analyst:adjudicate:r1", _SINGLE_BUNDLE, -1),
    ("self-collaboration-reading-analyst", "analyst", _READING_ANALYST_BUNDLE, -1),
    ("self-collaboration-reading-analyst", "coder:r1", _SINGLE_BUNDLE, -1),
    ("self-collaboration-reading-analyst", "tester:r1", _SCRIPTED_TESTER_BUNDLE, -1),
    ("self-collaboration-reading-analyst", "analyst:adjudicate:r1", _SINGLE_BUNDLE, -1),
)


class _ToolStub:
    def __init__(self, name: str):
        self.name = name


def _seat_agent(tool_names: tuple[str, ...]) -> FakeAgent:
    schemas = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in tool_names
    ]
    agent = FakeAgent(schemas)
    agent.tools = [_ToolStub(name) for name in tool_names]
    return agent


def _client_over(fake: _FakeChatClient, *, stream: bool) -> LLMClient:
    client = LLMClient(model=MODEL, api_key="unused", base_url="http://fake.invalid/v1", stream_chat=stream)
    client._openai = fake
    return client


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "unstreamed"])
@pytest.mark.parametrize(
    ("arm", "seat", "tool_names", "aid"),
    ARM_SEATS,
    ids=[f"{arm}:{seat}" for arm, seat, _tools, _aid in ARM_SEATS],
)
def test_every_arm_seat_records_transport_timing_on_its_llm_call(
    arm: str, seat: str, tool_names: tuple[str, ...], aid: int, stream: bool
):
    tracer = FakeTracer()
    fake = _FakeChatClient(stream=stream)
    runner = build_runner(
        state=SessionState(messages=[{"role": "system", "content": "sys"}], aid=aid),
        tracer=tracer,
        llm=_client_over(fake, stream=stream),
        agent=_seat_agent(tool_names),
        max_budget_tokens=1_000_000,
        max_steps=4,
    )

    run(runner.run_loop())

    payloads = [s["payload"] for s in tracer.steps if s["step_type"] == "llm_call"]
    assert payloads, f"{arm}/{seat} produced no llm_call record"
    for payload in payloads:
        block = _assert_block_shape(payload.get("transport_timing"))
        # Presence is the contract; measurability is a property of the wire
        # format, and the record says which of the two it is either way.
        assert block["streamed"] is stream
        assert block["ttft_measured"] is stream
        assert (block["ttft_s"] is not None) is stream


def test_transport_timing_never_replaces_an_existing_field():
    """Readers of six batches of ``metrics.jsonl`` and of the trajectories run
    on the old keys; this change may only add."""
    tracer = FakeTracer()
    runner = build_runner(
        state=SessionState(messages=[{"role": "system", "content": "sys"}], aid=0),
        tracer=tracer,
        llm=_client_over(_FakeChatClient(stream=True), stream=True),
        agent=_seat_agent(_SINGLE_BUNDLE),
        max_budget_tokens=1_000_000,
        max_steps=4,
    )

    run(runner.run_loop())

    step = next(s for s in tracer.steps if s["step_type"] == "llm_call")
    assert {"aid", "model", "finish_reason", "content", "tool_calls", "usage"} <= set(step["payload"])
    assert step["latency"] > 0
    assert step["payload"]["transport_timing"]["ttft_s"] < step["latency"]


def test_usage_ledger_row_gains_the_block_and_keeps_every_old_key(tmp_path, monkeypatch):
    """``api_usage.jsonl`` is read by OpenCollab-Eval's token/cost summary, which
    keys off ``schema`` and ``usage``. The row may grow, never change shape."""
    log = tmp_path / "api_usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(log))

    client, _fake = _chat_client(stream=True)
    run(client.complete(MESSAGES))

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["schema"] == "opencollab.api_usage.v1"
    assert {"timestamp", "request_id", "status", "provider", "model", "latency_s", "usage"} <= set(row)
    block = _assert_block_shape(row["transport_timing"])
    assert block["ttft_measured"] is True
    assert block["ttft_s"] < row["latency_s"]


def test_a_call_that_never_reached_the_provider_says_so():
    """``None`` for TTFT has two causes and they are not the same fact: the
    response was not streamed, or no request was ever issued."""
    timer = FirstTokenTimer()
    block = timer.snapshot()

    assert set(block) == set(TRANSPORT_TIMING_KEYS)
    assert block["attempts"] == 0
    assert block["ttft_measured"] is False
    assert block["ttft_unavailable_reason"] == "no_provider_attempt_started"
    assert block["streamed"] is None
    assert block["request_started_at"] is None
    assert block["attempt_s"] is None
