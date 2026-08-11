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
_REQUEST_UPPER_BOUND_MESSAGE_FIELDS = _REQUEST_MESSAGE_FIELDS | {"provider_state"}
_MESSAGE_TOKEN_OVERHEAD = 4
_TOOLS_TOKEN_OVERHEAD = 4
_REQUEST_PROTOCOL_TOKEN_OVERHEAD = 16
_MESSAGE_PROTOCOL_TOKEN_OVERHEAD = 64
_TOOLS_PROTOCOL_TOKEN_OVERHEAD = 16
_TOOL_PROTOCOL_TOKEN_OVERHEAD = 32


def _serialize_payload(value: Any) -> str:
    """Serialize a provider payload deterministically."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _serialized_tokens(value: Any) -> int:
    """Estimate a provider payload after deterministic JSON serialization."""
    return estimate_tokens(_serialize_payload(value))


def _serialized_token_upper_bound(value: Any) -> int:
    """Bound byte-backed tokenization by charging one token per UTF-8 byte."""
    return len(_serialize_payload(value).encode("utf-8"))


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


def request_tokens_upper_bound(
    messages: list[dict], tools: list[dict] | None = None
) -> int:
    """Return a conservative upper bound for provider request input tokens.

    Every serialized UTF-8 byte is reserved as one token. Explicit request,
    message, and tool framing allowances cover provider envelopes and
    normalization tokens outside the serialized source payload. Provider state
    is included because Anthropic replays its native content blocks directly.
    """
    total = _REQUEST_PROTOCOL_TOKEN_OVERHEAD
    for message in messages:
        payload = {
            key: ("" if key == "content" and value is None else value)
            for key, value in message.items()
            if key in _REQUEST_UPPER_BOUND_MESSAGE_FIELDS
        }
        total += _MESSAGE_PROTOCOL_TOKEN_OVERHEAD + _serialized_token_upper_bound(
            payload
        )
    if tools:
        total += (
            _TOOLS_PROTOCOL_TOKEN_OVERHEAD
            + len(tools) * _TOOL_PROTOCOL_TOKEN_OVERHEAD
            + _serialized_token_upper_bound(tools)
        )
    return total
