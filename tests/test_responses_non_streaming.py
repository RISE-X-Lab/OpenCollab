"""Non-streaming response parsing tests for the Responses adapter."""

import pytest
from responses_provider_test_support import completed_response, message_item

from opencollab.adapters.llm.responses_provider import (
    ResponsesProtocolError,
    parse_responses_response,
)


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
