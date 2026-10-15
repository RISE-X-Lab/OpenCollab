"""Context decisions must size replayed input rather than saved reasoning."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from session_run_loop_test_support import FakeAgent, FakeLLM, build_runner, llm_response, run

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.openai_provider import _build_request_kwargs
from opencollab.adapters.llm.responses_messages import messages_to_input
from opencollab.application.shaping import approx_messages_tokens
from opencollab.bootstrap.container import _build_default_shaper
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.token_estimation import estimate_request_message_tokens, estimate_request_tokens


@pytest.fixture
def make_client():
    clients = []

    def create(*, stream=True, provider="openai", wire_protocol="chat_completions"):
        client = LLMClient(
            model="context-estimate-test-model",
            api_key="unused-local-test",
            provider=provider,
            wire_protocol=wire_protocol,
            stream_chat=stream,
            context_window=34_000,
        )
        clients.append(client)
        return client

    yield create
    for client in clients:
        asyncio.run(client.close())


def _history():
    return [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Fix the failing test."},
        {
            "role": "assistant",
            "content": "I located the failing assertion.",
            "reasoning_content": "UNIQUE_REASONING_ONLY_FACT " + "reasoning " * 20_000,
        },
        {"role": "user", "content": "Continue."},
        {"role": "assistant", "content": "I will edit the implementation."},
    ]


@pytest.mark.parametrize("stream", [True, False])
def test_chat_estimate_matches_normalized_request_and_ignores_local_state(make_client, stream):
    client = make_client(stream=stream)
    messages = _history()
    messages[2]["provider_state"] = {"unused": "local state " * 20_000}
    original = deepcopy(messages)
    request = _build_request_kwargs(client.model, messages, None, 0.0, keep_reasoning_content=not stream)

    assert client.estimate_input_message_tokens(messages) == estimate_request_message_tokens(request["messages"])
    assert client.estimate_input_message_tokens(messages) == sum(
        client.estimate_input_message_tokens([message]) for message in messages
    )
    assert messages == original


@pytest.mark.parametrize("stream", [True, False])
def test_default_compaction_follows_reasoning_replay_and_reports_same_estimate(make_client, stream):
    client = make_client(stream=stream)
    messages = _history()
    original = deepcopy(messages)
    summaries = []

    def summarize(segment):
        summaries.append(segment)
        return "Visible work preserved."

    shaper = _build_default_shaper(client, summarize)
    shaped, reports = shaper.shape_with_report(messages)

    assert approx_messages_tokens(messages) > 1_000
    assert reports[0]["tokens_before"] == client.estimate_input_message_tokens(messages)
    assert reports[-1]["tokens_after"] == client.estimate_input_message_tokens(shaped)
    assert messages == original
    if stream:
        assert client.estimate_input_message_tokens(messages) < 1_000
        assert shaped == original
        assert summaries == []
        assert [report["rung"] for report in reports] == ["none"]
    else:
        assert len(summaries) == 1
        assert [report["rung"] for report in reports] == ["auto_compact"]
        assert any(message.get("compacted") for message in shaped)


def test_real_visible_input_still_triggers_compaction_when_streaming(make_client):
    client = make_client()
    messages = _history()
    messages[2]["content"] = "visible model input " * 20_000
    summaries = []

    def summarize(segment):
        summaries.append(segment)
        return "Summary of visible work."

    shaped, reports = _build_default_shaper(client, summarize).shape_with_report(messages)

    assert len(summaries) == 1
    assert [report["rung"] for report in reports] == ["auto_compact"]
    assert reports[0]["tokens_before"] == client.estimate_input_message_tokens(messages)
    assert reports[0]["tokens_after"] == client.estimate_input_message_tokens(shaped)
    assert reports[0]["tokens_after"] < reports[0]["tokens_before"]


def test_anthropic_counts_native_thinking_once_and_keeps_signed_tool_state(make_client):
    client = make_client(provider="anthropic")
    thinking = {"type": "thinking", "thinking": "native thought " * 6_000, "signature": "signed-state"}
    native = [thinking, {"type": "text", "text": "I will inspect the file."}]
    message = {
        "role": "assistant",
        "content": "I will inspect the file.",
        "reasoning_content": thinking["thinking"],
        "provider_state": {"anthropic_content": native},
    }
    original = deepcopy(message)
    expected = estimate_request_message_tokens([{"role": "assistant", "content": native}])

    assert client.estimate_input_message_tokens([message]) == expected
    assert message == original
    message["reasoning_content"] = "display-only copy " * 20_000
    assert client.estimate_input_message_tokens([message]) == expected
    message["provider_state"]["anthropic_content"] = native[1:]
    assert client.estimate_input_message_tokens([message]) < 1_000 < expected
    assert original["provider_state"]["anthropic_content"][0] == thinking


def test_anthropic_group_estimates_are_additive_and_do_not_mutate_history(make_client):
    client = make_client(provider="anthropic")
    messages = [
        {"role": "system", "content": "Instructions."},
        {"role": "user", "content": "Fix the test."},
        {
            "role": "assistant",
            "content": "I will inspect the file.",
            "provider_state": {"anthropic_content": [
                {"type": "thinking", "thinking": "native thought", "signature": "signature"},
                {"type": "tool_use", "id": "a", "name": "file_read", "input": {"path": "test.py"}},
            ]},
        },
        {"role": "tool", "tool_call_id": "a", "content": "Assertion."},
        {"role": "tool", "tool_call_id": "b", "content": "More output."},
    ]
    original = deepcopy(messages)

    assert client.estimate_input_message_tokens(messages) == sum(
        client.estimate_input_message_tokens([message]) for message in messages
    )
    assert messages == original


def test_responses_counts_replayed_native_items_without_display_copies(make_client):
    client = make_client(wire_protocol="responses")
    replay = [
        {"type": "reasoning", "id": "r", "encrypted_content": "encrypted-state" * 6_000, "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Visible answer."}]},
    ]
    message = {
        "role": "assistant",
        "content": "Visible answer.",
        "reasoning_content": "display reasoning " * 20_000,
        "response_items": replay,
    }
    original = deepcopy(message)
    estimate = client.estimate_input_message_tokens([message])

    assert estimate == estimate_request_message_tokens([{"role": "assistant", "content": replay}])
    assert estimate > 1_000
    assert message == original
    message["content"] = "different display copy " * 20_000
    message["reasoning_content"] = "different reasoning copy " * 20_000
    assert client.estimate_input_message_tokens([message]) == estimate


@pytest.mark.parametrize("part_type", ["text", "input_text", "output_text", "image_url"])
def test_responses_legacy_block_metadata_cannot_trigger_compaction(make_client, part_type):
    client = make_client(wire_protocol="responses")
    messages = _history()
    block = {"type": part_type, "text": "Visible answer."}
    if part_type == "image_url":
        block = {"type": part_type, "image_url": {"url": "https://example.test/image.png", "detail": "high"}}
    messages[2]["content"] = [block]
    sent_before = messages_to_input(messages)
    estimate_before = client.estimate_input_message_tokens(messages)
    block["annotations"] = "LOCAL_DISPLAY_ANNOTATION " * 10_000
    if part_type == "image_url":
        block["image_url"]["local_metadata"] = block["annotations"]
    original = deepcopy(messages)
    summaries = []

    shaped, reports = _build_default_shaper(
        client, lambda segment: summaries.append(segment) or "Summary."
    ).shape_with_report(messages)

    assert messages_to_input(messages) == sent_before
    assert client.estimate_input_message_tokens(messages) == estimate_before < 1_000
    assert summaries == []
    assert [report["rung"] for report in reports] == ["none"]
    assert reports[0]["tokens_before"] == reports[0]["tokens_after"] == estimate_before
    assert shaped == messages == original


def test_responses_tool_fragments_use_native_fields_and_keep_additive_estimates(make_client):
    client = make_client(wire_protocol="responses")
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "Inspect the test."},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_one", "type": "function",
                "function": {"name": "file_read", "arguments": '{"path":"test.py"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_one", "content": [{"type": "text", "text": "Assertion."}]},
    ]
    sent_before = messages_to_input(messages)
    estimate_before = client.estimate_input_message_tokens(messages)
    messages[2]["tool_calls"][0]["function"]["local_metadata"] = "local data " * 20_000
    messages[3]["content"][0]["annotations"] = "local data " * 20_000
    messages[3]["name"] = "local tool name " * 20_000
    original = deepcopy(messages)

    assert messages_to_input(messages) == sent_before
    assert client.estimate_input_message_tokens(messages) == estimate_before
    assert estimate_before == sum(client.estimate_input_message_tokens([message]) for message in messages)
    assert messages == original
    messages[3]["content"][0]["text"] = "real tool output " * 20_000
    assert client.estimate_input_message_tokens(messages) > estimate_before + 1_000


def test_injected_client_without_estimator_keeps_conservative_compaction():
    summaries = []
    llm = SimpleNamespace(context_window=lambda: 34_000)
    shaper = _build_default_shaper(llm, lambda segment: summaries.append(segment) or "Summary.")

    shaped, reports = shaper.shape_with_report(_history())

    assert len(summaries) == 1
    assert reports[0]["tokens_before"] == approx_messages_tokens(_history())
    assert any(message.get("compacted") for message in shaped)


@pytest.mark.parametrize("stream", [True, False])
def test_budget_reservation_uses_client_policy_even_when_agent_flag_differs(make_client, stream):
    client = make_client(stream=stream)
    messages = _history()
    visible_input = estimate_request_tokens(messages, keep_reasoning_content=False)
    llm = FakeLLM([llm_response(content="done", input_tokens=10, total_tokens=20)])
    llm.estimate_input_message_tokens = client.estimate_input_message_tokens
    agent = FakeAgent()
    agent.llm_stream_chat = not stream
    state = SessionState(messages=messages)
    runner = build_runner(state=state, agent=agent, llm=llm, max_budget_tokens=visible_input + 2_000)

    result = run(runner.run_loop())

    if stream:
        assert result == "done"
        assert len(llm.calls) == 1
    else:
        assert result == ""
        assert llm.calls == []
        assert state.phase is SessionPhase.STOPPED
        assert state.terminal_reason.startswith("budget exhausted before model call")
