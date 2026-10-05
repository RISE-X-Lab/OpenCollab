"""Usage projection for the OpenAI Responses transport."""

from __future__ import annotations

from typing import Any

from opencollab.adapters.llm._attempt_usage import (  # noqa: F401 - compatibility re-export
    _combine_attempt_usage as _combine_responses_usage,
)
from opencollab.adapters.llm._attempt_usage import _optional_usage_int
from opencollab.adapters.llm.types import (
    Usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_to_dict,
)


def _positive_usage_int(source: Any, key: str) -> int | None:
    value = _optional_usage_int(source, key)
    return value if value is not None and value > 0 else None


def _reported_responses_usage(response: Any) -> Usage | None:
    """Retain only counters actually returned by a failed attempt."""
    raw = usage_to_dict(getattr(response, "usage", None))
    input_tokens = _optional_usage_int(raw, "input_tokens")
    output_tokens = _optional_usage_int(raw, "output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    cache_creation = _optional_usage_int(input_details, "cache_write_tokens")
    if cache_creation is None:
        cache_creation = _optional_usage_int(raw, "cache_write_tokens")
    return Usage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        cache_read_tokens=_optional_usage_int(input_details, "cached_tokens"),
        cache_creation_tokens=cache_creation,
        reasoning_tokens=_optional_usage_int(output_details, "reasoning_tokens"),
        estimated=input_tokens is None or output_tokens is None,
        raw_usage=raw,
    )


def parse_responses_usage(
    response: Any,
    messages: list[dict[str, Any]],
    content: str | None,
    tool_calls: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> Usage:
    """Build normalized usage while retaining provider-native counters.

    ``input_tokens`` is estimated when a Responses endpoint omits its usage
    counters.  The request's registered tool schemas are part of that input,
    so callers must pass the provider-shaped tool list through to the
    estimator as well as the conversational messages.
    """
    raw = usage_to_dict(getattr(response, "usage", None))
    input_tokens = _positive_usage_int(raw, "input_tokens")
    output_tokens = _positive_usage_int(raw, "output_tokens")
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    cache_creation_tokens = _optional_usage_int(input_details, "cache_write_tokens")
    if cache_creation_tokens is None:
        cache_creation_tokens = _optional_usage_int(raw, "cache_write_tokens")
    estimated = input_tokens is None or output_tokens is None
    if input_tokens is None:
        input_tokens = estimate_messages_tokens(
            messages,
            tools,
            prefer_response_items=True,
        )
    if output_tokens is None:
        text = content or ""
        for call in tool_calls:
            function = call["function"]
            text += str(function.get("name") or "") + str(function.get("arguments") or "")
        output_tokens = estimate_tokens(text) if text else 0
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=_optional_usage_int(input_details, "cached_tokens"),
        cache_creation_tokens=cache_creation_tokens,
        reasoning_tokens=_optional_usage_int(output_details, "reasoning_tokens"),
        estimated=estimated,
        raw_usage=raw,
    )


__all__ = ["parse_responses_usage"]
