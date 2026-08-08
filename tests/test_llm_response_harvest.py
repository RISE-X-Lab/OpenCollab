"""Unit tests for provider response harvesting — reasoning, thinking blocks, markup recovery."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from llm_provider_test_support import _openai_resp

from opencollab.adapters.llm.anthropic_provider import _parse_response as parse_anthropic_response
from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.adapters.llm.openai_provider import _parse_response as parse_openai_response
from opencollab.adapters.llm.openai_provider import _usage_int as openai_usage_int

# ---------------------------------------------------------------------------
# reasoning_content harvest — rescue a genuinely empty thinking turn
# ---------------------------------------------------------------------------


def _openai_resp_with_reasoning(content, reasoning_content, tool_calls=None):
    message = SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning_content
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


def test_openai_harvests_reasoning_content_when_content_empty():
    """An empty-content, no-tool-call turn falls back to reasoning_content."""
    resp = _openai_resp_with_reasoning(content=None, reasoning_content="the answer is 42")
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content == "the answer is 42"
    assert result.reasoning == "the answer is 42"  # also recorded for the trajectory


def test_openai_keeps_real_content_over_reasoning():
    """When content is present it wins — but reasoning is still recorded."""
    resp = _openai_resp_with_reasoning(content="real answer", reasoning_content="scratch work")
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content == "real answer"
    assert result.reasoning == "scratch work"  # kept for trajectory observability


def test_openai_does_not_harvest_reasoning_on_tool_call_turn():
    """A tool-call turn legitimately has empty content; don't inject reasoning."""
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="run", arguments="{}"))
    resp = _openai_resp_with_reasoning(content=None, reasoning_content="thinking", tool_calls=[tc])
    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])
    assert result.content is None
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "run"


# ---------------------------------------------------------------------------
# Anthropic thinking-block harvest — mirror of the OpenAI reasoning harvest
# ---------------------------------------------------------------------------


def _anthropic_resp(blocks, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    return SimpleNamespace(content=blocks, usage=usage, stop_reason=stop_reason)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _thinking_block(text, signature=None):
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def _tool_use_block(name="run", input=None):
    return SimpleNamespace(type="tool_use", id="c1", name=name, input=input or {})


def test_anthropic_harvests_thinking_when_no_text():
    """An empty-text, no-tool turn falls back to the thinking-block text."""
    resp = _anthropic_resp([_thinking_block("the answer is 42")])
    result = parse_anthropic_response(resp)
    assert result.content == "the answer is 42"
    assert result.reasoning == "the answer is 42"  # also recorded for the trajectory


def test_anthropic_keeps_real_text_over_thinking():
    """When a text block is present it wins — but thinking is still recorded."""
    resp = _anthropic_resp([_thinking_block("scratch"), _text_block("real answer")])
    result = parse_anthropic_response(resp)
    assert result.content == "real answer"
    assert result.reasoning == "scratch"  # kept for trajectory observability


def test_anthropic_does_not_harvest_thinking_on_tool_call_turn():
    """A tool-call turn legitimately has empty text; don't inject thinking."""
    resp = _anthropic_resp([_thinking_block("plan"), _tool_use_block(name="run")])
    result = parse_anthropic_response(resp)
    assert result.content is None
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "run"


def test_anthropic_preserves_signed_thinking_for_tool_follow_up():
    resp = _anthropic_resp([
        _thinking_block("plan", signature="signed-thinking"),
        _tool_use_block(name="run"),
    ])

    result = parse_anthropic_response(resp)
    message = {
        "role": "assistant",
        "tool_calls": result.tool_calls,
        "provider_state": result.provider_state,
    }
    _, converted = convert_to_anthropic_messages([message])

    assert converted[0]["content"][0] == {
        "type": "thinking",
        "thinking": "plan",
        "signature": "signed-thinking",
    }
    assert converted[0]["content"][1]["type"] == "tool_use"


def test_anthropic_conversion_normalizes_openai_text_blocks_without_mutation():
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "system "},
                {"type": "text", "text": "policy"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        },
    ]
    original = json.loads(json.dumps(messages))

    system, converted = convert_to_anthropic_messages(messages)

    assert system == ["system policy"]
    assert converted == [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ],
    }]
    assert messages == original


def test_anthropic_conversion_rejects_unsupported_content_block_with_location():
    messages = [
        {"role": "user", "content": "before"},
        {
            "role": "assistant",
            "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
        },
    ]

    with pytest.raises(
        ValueError,
        match=r"message 1 .*assistant.* unsupported content block type: image_url",
    ):
        convert_to_anthropic_messages(messages)


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ('{"path":', "invalid JSON"),
        ('"path"', "JSON object"),
        ("[]", "JSON object"),
        (b"{}", "text or an object"),
        (None, "text or an object"),
    ],
)
def test_anthropic_conversion_rejects_invalid_restored_tool_arguments(
    arguments,
    reason,
):
    messages = [
        {"role": "user", "content": "before"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-bad",
                "type": "function",
                "function": {"name": "file_read", "arguments": arguments},
            }],
        },
    ]

    with pytest.raises(
        ValueError,
        match=rf"message 1 .*file_read.*call-bad.*{reason}",
    ):
        convert_to_anthropic_messages(messages)


def test_anthropic_conversion_preserves_object_tool_arguments():
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call-ok",
            "type": "function",
            "function": {
                "name": "file_read",
                "arguments": '{"path": "src/a.py", "options": {"line": 2}}',
            },
        }],
    }]

    _, converted = convert_to_anthropic_messages(messages)

    assert converted[0]["content"][0]["input"] == {
        "path": "src/a.py",
        "options": {"line": 2},
    }


def test_anthropic_redacted_thinking_is_not_harvested():
    """redacted_thinking holds encrypted data, not text — must not become content."""
    redacted = SimpleNamespace(type="redacted_thinking", data="encrypted-bytes")
    resp = _anthropic_resp([redacted])
    result = parse_anthropic_response(resp)
    assert result.content is None
    assert result.provider_state == {
        "anthropic_content": [
            {"type": "redacted_thinking", "data": "encrypted-bytes"}
        ]
    }


# ---------------------------------------------------------------------------
# P6 — kimi literal tool-call markup recovery (finish_reason='stop', no
# parsed tool_calls; the call is embedded as special-token text in content)
# ---------------------------------------------------------------------------


def _markup(name, call_id, args_json):
    return (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>"
        f"functions.{name}:{call_id}"
        "<|tool_call_argument_begin|>"
        f"{args_json}"
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )


def test_openai_markup_single_tool_call_is_synthesized():
    """A single markup tool call -> one synthesized tool_call, content cleared."""
    content = _markup("grep", "call_1", '{"pattern": "foo"}')
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "grep"
    assert json.loads(tc["function"]["arguments"]) == {"pattern": "foo"}
    assert result.content is None
    assert result.usage.markup_recovered == 1  # P6 recovery counter fires


def test_openai_markup_two_tool_calls_are_synthesized():
    """Two markup blocks -> two synthesized tool_calls in order."""
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.grep:c1"
        '<|tool_call_argument_begin|>{"pattern": "a"}<|tool_call_end|>'
        "<|tool_call_begin|>functions.read_file:c2"
        '<|tool_call_argument_begin|>{"path": "x.py"}<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert [tc["function"]["name"] for tc in result.tool_calls] == ["grep", "read_file"]
    assert [tc["id"] for tc in result.tool_calls] == ["c1", "c2"]
    assert result.content is None


def test_openai_markup_malformed_group_is_not_partially_executed():
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.file_read:c1"
        '<|tool_call_argument_begin|>{"path": "safe.txt"}<|tool_call_end|>'
        "<|tool_call_begin|>functions.file_write:c2"
        '<|tool_call_argument_begin|>{"path": "target.txt"<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert result.tool_calls == []
    assert result.content == content
    assert result.usage.markup_recovered == 0


def test_openai_markup_preserves_surrounding_prose():
    """Genuine prose around the markup is preserved; the call is still synthesized."""
    content = "Let me search the repo. " + _markup("grep", "c1", '{"pattern": "foo"}')
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "grep"
    assert result.content == "Let me search the repo."


def test_openai_malformed_markup_falls_back_gracefully():
    """Partial markup / non-JSON args -> no crash, no synthesized call, content kept."""
    content = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.grep:c1"
        "<|tool_call_argument_begin|>not-json-args"  # truncated, invalid JSON
    )
    resp = _openai_resp(usage=None, content=content)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert result.tool_calls == []
    assert result.content == content  # unchanged fallback
    assert result.usage.markup_recovered == 0  # nothing recovered -> counter stays 0


def test_openai_real_tool_calls_skip_markup_parsing():
    """When the SDK already parsed tool_calls, markup recovery is not attempted."""
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="run", arguments="{}"))
    resp = _openai_resp(usage=None, content="irrelevant", tool_calls=[tc])

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "run"
    assert result.content == "irrelevant"
    assert result.usage.markup_recovered == 0  # structured calls -> no recovery


def test_openai_markup_in_reasoning_content_is_synthesized():
    """Under thinking mode kimi puts the tool-call markup in reasoning_content
    (content empty). Recover it from reasoning, not just content."""
    resp = _openai_resp_with_reasoning(
        content=None, reasoning_content=_markup("file_read", "2", '{"path": "a.py"}')
    )

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "file_read"
    assert result.tool_calls[0]["id"] == "2"
    # the markup must NOT leak into content via the reasoning rescue
    assert result.content is None
    assert result.reasoning is None  # whole reasoning was markup -> stripped
    assert result.usage.markup_recovered == 1  # recovered from reasoning_content


def test_openai_markup_buried_in_reasoning_preserves_thinking():
    """Real thinking prose followed by buried markup -> call synthesized, the
    genuine chain-of-thought is preserved in reasoning."""
    reasoning = "Let me check the test.\n\n" + _markup("grep", "1", '{"pattern": "X"}')
    resp = _openai_resp_with_reasoning(content=None, reasoning_content=reasoning)

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "grep"
    assert result.content is None
    assert result.reasoning == "Let me check the test."


def test_openai_content_markup_takes_precedence_over_reasoning():
    """If markup is in content, parse there; don't double-parse reasoning."""
    resp = _openai_resp_with_reasoning(
        content=_markup("grep", "c1", '{"pattern": "a"}'),
        reasoning_content=_markup("read_file", "c2", '{"path": "x"}'),
    )

    result = parse_openai_response(resp, [{"role": "user", "content": "q"}])

    assert [tc["function"]["name"] for tc in result.tool_calls] == ["grep"]
    assert result.content is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, "-2"])
def test_openai_usage_rejects_non_finite_and_negative_values(value):
    assert openai_usage_int({"tokens": value}, "tokens") == 0
