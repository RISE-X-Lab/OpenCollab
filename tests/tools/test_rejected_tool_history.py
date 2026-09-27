"""Malformed tool calls stay in the transcript and have a bounded model view."""

from __future__ import annotations

import copy
import json

import pytest

from opencollab.adapters.llm.responses_messages import messages_to_input
from opencollab.application.shaping import PerToolResultBudgetShaper


def messages(arguments, error="Error: invalid JSON arguments: malformed"):
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": arguments}}],
            "response_items": [
                {"id": "item-1", "type": "function_call", "call_id": "call-1", "name": "bash", "arguments": arguments}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": error},
    ]


@pytest.mark.parametrize(
    "arguments", ['{"command": "' + "x" * 100_000, json.dumps(["x" * 100_000])], ids=["malformed", "nonobject"]
)
def test_rejected_arguments_shrink_both_provider_representations(arguments):
    source = messages(
        arguments,
        "Error: invalid JSON arguments: malformed"
        if arguments.startswith("{")
        else "Error: tool arguments must be a JSON object: list",
    )
    original = copy.deepcopy(source)
    result = PerToolResultBudgetShaper().shape(source)
    assert len(json.dumps(result)) < 2000
    assert source == original
    assert result[0]["tool_calls"][0]["function"]["arguments"] == result[0]["response_items"][0]["arguments"]
    _, projected = messages_to_input(result)
    assert [x["type"] for x in projected] == ["function_call", "function_call_output"]
    assert projected[0]["call_id"] == projected[1]["call_id"] == "call-1"


def test_large_valid_arguments_remain_exact_even_if_tool_output_mentions_json_error():
    source = messages(json.dumps({"command": "x" * 100_000}))
    assert PerToolResultBudgetShaper().shape(source) == source


def test_pending_malformed_call_is_not_modified_before_a_tool_result():
    source = messages('{"command":"' + "x" * 100_000)[:1]
    assert PerToolResultBudgetShaper().shape(source) == source


def test_reused_call_id_does_not_change_an_earlier_unrejected_call():
    first = messages('{"command":"' + "a" * 100_000, "tool transport failed")
    last = messages('{"command":"' + "b" * 100_000)
    result = PerToolResultBudgetShaper().shape(first + last)
    assert result[:2] == first
    assert len(json.dumps(result[2:])) < 2000
