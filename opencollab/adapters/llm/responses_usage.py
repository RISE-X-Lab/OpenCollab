"""Usage projection for the OpenAI Responses transport."""

from __future__ import annotations

from typing import Any

from opencollab.adapters.llm.types import (
    Usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_to_dict,
)


def _optional_usage_int(source: Any, key: str) -> int | None:
    if not isinstance(source, dict) or source.get(key) is None:
        return None
    if isinstance(source[key], bool):
        return None
    try:
        value = int(source[key])
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _positive_usage_int(source: Any, key: str) -> int | None:
    value = _optional_usage_int(source, key)
    return value if value is not None and value > 0 else None


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
        input_tokens = estimate_messages_tokens(messages, tools)
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
