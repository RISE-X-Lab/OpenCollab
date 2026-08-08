"""Dependency-free estimates for structured provider request payloads."""

from __future__ import annotations

import json
from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English, ~2 for CJK."""
    return max(1, len(text) // 3)


_REQUEST_MESSAGE_FIELDS = frozenset({
    "role", "content", "reasoning_content", "tool_calls", "tool_call_id", "name",
})
_MESSAGE_TOKEN_OVERHEAD = 4
_TOOLS_TOKEN_OVERHEAD = 4


def _serialized_tokens(value: Any) -> int:
    """Estimate a provider payload after deterministic JSON serialization."""
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return estimate_tokens(serialized)


def estimate_messages_tokens(
    messages: list[dict], tools: list[dict] | None = None
) -> int:
    """Estimate OpenAI-compatible request tokens, including structured payloads.

    Usage fallbacks and history compaction receive assistant tool calls, tool
    response IDs, and thinking traces in addition to ordinary text. Estimate
    the same protocol fields that the provider request normalizer transmits,
    then include registered tool schemas when they are part of the request.
    """
    total = 0
    for message in messages:
        payload = {
            key: ("" if key == "content" and value is None else value)
            for key, value in message.items()
            if key in _REQUEST_MESSAGE_FIELDS
        }
        total += _MESSAGE_TOKEN_OVERHEAD + _serialized_tokens(payload)
    if tools:
        total += _TOOLS_TOKEN_OVERHEAD + _serialized_tokens(tools)
    return total
