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


def test_responses_native_items_are_estimated_additively_and_survive_reasoning_policy():
    from opencollab.adapters.llm.client import LLMClient

    native = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "e" * 60_000,
        "summary": [],
    }
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "visible answer",
            "reasoning_content": "chat trace",
            "response_items": [native],
        },
    ]
    without_native = [messages[0], {key: value for key, value in messages[1].items() if key != "response_items"}]
    native_only = [messages[0], {"role": "assistant", "response_items": [native]}]

    assert estimate_request_tokens(messages) > estimate_request_tokens(without_native) + 10_000
    assert estimate_request_tokens(messages) > estimate_request_tokens(native_only)
    assert estimate_request_tokens(messages, prefer_response_items=True) == estimate_request_tokens(
        native_only, prefer_response_items=True
    )
    assert estimate_request_message_tokens(messages) == sum(
        estimate_request_message_tokens([message]) for message in messages
    )
    assert estimate_request_tokens(messages, keep_reasoning_content=False) > estimate_request_tokens(
        without_native, keep_reasoning_content=False
    )
    assert estimate_request_tokens(
        messages, keep_reasoning_content=False, prefer_response_items=True
    ) == estimate_request_tokens(
        native_only, keep_reasoning_content=False, prefer_response_items=True
    )

    from opencollab.adapters.llm.responses_provider import _messages_to_input

    _, replayed = _messages_to_input([messages[1]])
    assert replayed == [native]

    client = LLMClient(model="gpt-5", api_key="unused", wire_protocol="responses")  # pragma: allowlist secret
    assert client.estimate_request_tokens(messages) == estimate_request_tokens(
        messages, prefer_response_items=True
    )


def test_responses_precedence_keeps_system_and_tool_payloads_ahead_of_native_items():
    from opencollab.adapters.llm.responses_provider import _messages_to_input

    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": "{}",
    }
    system = {"role": "system", "content": "instructions " * 10_000, "response_items": []}
    native_assistant = {"role": "assistant", "response_items": [call]}
    tool = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "tool result " * 10_000,
        "response_items": [],
    }
    instructions, items = _messages_to_input([system, native_assistant, tool])

    assert instructions == system["content"]
    assert items == [call, {"type": "function_call_output", "call_id": "call_1", "output": tool["content"]}]
    assert estimate_request_message_tokens([system], prefer_response_items=True) == estimate_request_message_tokens(
        [{key: value for key, value in system.items() if key != "response_items"}],
        prefer_response_items=True,
    )
    assert estimate_request_message_tokens([tool], prefer_response_items=True) == estimate_request_message_tokens(
        [{key: value for key, value in tool.items() if key != "response_items"}],
        prefer_response_items=True,
    )


@pytest.mark.parametrize("native_replay", ["absent", "empty", "present"])
def test_responses_developer_precedence_matches_adapter_replay_order(native_replay):
    from opencollab.adapters.llm.responses_provider import _messages_to_input

    native = {"type": "reasoning", "id": "rs_1", "encrypted_content": "cipher", "summary": []}
    developer = {"role": "developer", "content": "developer content" * 1_000}
    if native_replay != "absent":
        developer["response_items"] = [native] if native_replay == "present" else []

    instructions, items = _messages_to_input([developer])
    estimated = estimate_request_message_tokens([developer], prefer_response_items=True)
    if native_replay == "present":
        assert instructions is None
        assert items == [native]
        assert estimated == estimate_request_message_tokens(
            [{"role": "developer", "response_items": [native]}],
            prefer_response_items=True,
        )
    elif native_replay == "empty":
        assert instructions is None
        assert items == []
        assert estimated == estimate_request_message_tokens(
            [{"role": "developer", "response_items": []}],
            prefer_response_items=True,
        )
    else:
        assert instructions is None
        assert items == [{"role": "developer", "content": developer["content"]}]
        assert estimated == estimate_request_message_tokens([developer])


def test_chat_restore_with_empty_responses_items_counts_large_content():
    messages = [{"role": "assistant", "content": "c" * 60_000, "response_items": []}]
    content_only = [{"role": "assistant", "content": "c" * 60_000}]

    assert estimate_request_tokens(messages) > estimate_request_tokens(content_only)
    assert estimate_request_message_tokens(messages) > estimate_request_message_tokens(content_only)
    assert estimate_request_tokens(messages, prefer_response_items=True) < estimate_request_tokens(content_only)

    chat_client = LLMClient(model="gpt-4o", api_key="unused")  # pragma: allowlist secret
    responses_client = LLMClient(model="gpt-5", api_key="unused", wire_protocol="responses")  # pragma: allowlist secret
    assert chat_client.estimate_request_tokens(messages) == estimate_request_tokens(content_only)
    assert responses_client.estimate_request_tokens(messages) == estimate_request_tokens(
        messages, prefer_response_items=True
    )


def test_responses_native_items_activate_history_compaction_estimate():
    from opencollab.application.shaping.pipeline import approx_messages_tokens

    base = [{"role": "assistant", "content": "answer"}]
    with_native = [
        {
            **base[0],
            "response_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "x" * 60_000, "summary": []}
            ],
        }
    ]

    assert approx_messages_tokens(with_native) > approx_messages_tokens(base) + 10_000


def test_chat_restored_large_content_with_empty_native_items_triggers_history_sizing():
    from opencollab.application.shaping import AutoCompactShaper
    from opencollab.application.shaping.pipeline import approx_messages_tokens

    restored = [{"role": "assistant", "content": "c" * 60_000, "response_items": []}]
    content_only = [{"role": "assistant", "content": "c" * 60_000}]

    assert approx_messages_tokens(restored) > approx_messages_tokens(content_only)
    assert approx_messages_tokens(restored) > 10_000

    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "c" * 60_000, "response_items": []},
        {"role": "user", "content": "recent"},
    ]
    shaper = AutoCompactShaper(
        summarizer=lambda _segment: "summary",
        trigger_tokens=10_000,
        target_tokens=5_000,
        keep_recent_groups=1,
    )
    assert shaper.shape(history) is not history


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
