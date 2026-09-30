"""Streaming budget reservation follows the reasoning fields actually sent."""

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import (
    estimate_request_message_tokens,
    estimate_request_tokens,
)
from tests.runtime.test_llm_chat_streaming import FakeClient, chunk, usage_chunk
from tests.runtime.test_llm_chat_streaming_session import completion


def _reasoning_heavy_messages(reasoning_chars: int = 600_000):
    """A history whose input cost depends on the thinking replay policy."""
    return [
        {"role": "system", "content": "You are a careful engineer."},
        {"role": "user", "content": "Fix the failing test."},
        {
            "role": "assistant",
            "content": "Looking at the module now.",
            "reasoning_content": "step " * (reasoning_chars // 5),
        },
        {"role": "user", "content": "Continue."},
    ]


def _without_reasoning_content(messages):
    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


def test_reservation_without_reasoning_equals_reservation_of_stripped_history():
    """An explicit omission prices the same payload as stripped history."""
    messages = _reasoning_heavy_messages()
    stripped = _without_reasoning_content(messages)

    kept = estimate_request_tokens(messages)
    dropped = estimate_request_tokens(messages, keep_reasoning_content=False)

    assert dropped == estimate_request_tokens(stripped)
    assert dropped == estimate_request_tokens(
        stripped, keep_reasoning_content=False
    )
    # Not a marginal correction: recorded reasoning was the bulk of the input.
    assert kept > 3 * dropped

    # Same contract for the per-message half the shaping layers call.
    assert estimate_request_message_tokens(
        messages, keep_reasoning_content=False
    ) == estimate_request_message_tokens(stripped)


def test_reservation_default_still_counts_reasoning_content():
    """Generic estimates include all supplied continuation fields."""
    messages = _reasoning_heavy_messages(reasoning_chars=60_000)
    stripped = _without_reasoning_content(messages)

    assert estimate_request_tokens(messages) > estimate_request_tokens(stripped)
    assert estimate_request_message_tokens(
        messages
    ) > estimate_request_message_tokens(stripped)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True])
async def test_session_output_reservation_matches_actual_thinking_request(tmp_path, monkeypatch, stream, thinking):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    messages = _reasoning_heavy_messages(reasoning_chars=60_000)
    budget = estimate_request_tokens(messages) + 2_000
    script = [chunk(delta={"content": "done"}), chunk(finish_reason="stop"), usage_chunk()] \
        if stream else completion("done", [], "stop")
    wire = FakeClient([script])
    client = LLMClient(model="gpt-4o", api_key="unused", stream_chat=stream)
    await client._openai.close()
    client._openai = wire
    agent = Agent(
        name="coder", system_prompt="system", model="gpt-4o", thinking=thinking,
        thinking_params={"thinking": {"type": "enabled"}}, max_tokens_per_step=50_000,
    )
    session = build_session(
        agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)),
        max_steps=3, max_budget_tokens=budget,
    )
    session.state.messages = messages

    assert await session.run_loop() == "done"
    assert len(wire.calls) == 1
    request = wire.calls[0]
    reserved = estimate_request_tokens(request["messages"], request.get("tools"))
    assert request["max_tokens"] == budget - reserved
    assistant = next(message for message in request["messages"] if message["role"] == "assistant")
    assert ("reasoning_content" in assistant) is thinking


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True])
async def test_reasoning_heavy_history_stops_only_when_reasoning_is_sent(tmp_path, monkeypatch, stream, thinking):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    messages = _reasoning_heavy_messages(reasoning_chars=60_000)
    budget = estimate_request_tokens(messages, keep_reasoning_content=False) + 2_000
    assert budget < estimate_request_tokens(messages)
    script = [chunk(delta={"content": "done"}), chunk(finish_reason="stop"), usage_chunk()] \
        if stream else completion("done", [], "stop")
    wire = FakeClient([script])
    client = LLMClient(model="gpt-4o", api_key="unused", stream_chat=stream)
    await client._openai.close()
    client._openai = wire
    agent = Agent(name="coder", system_prompt="system", model="gpt-4o", thinking=thinking)
    session = build_session(
        agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)), max_budget_tokens=budget,
    )
    session.state.messages = messages

    assert await session.run_loop() == ("" if thinking else "done")
    assert len(wire.calls) == (0 if thinking else 1)
    assert session.phase is (SessionPhase.STOPPED if thinking else SessionPhase.DONE)
    if thinking:
        assert session.state.terminal_reason.startswith("budget exhausted before model call")
