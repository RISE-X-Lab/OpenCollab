"""Dependency-free estimates for structured provider request payloads."""

from __future__ import annotations

import json
from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~3 chars per token for ASCII, ~1 for the rest.

    Dividing every character by three understates CJK text roughly threefold,
    because those scripts run near one token per character; ASCII stays on the
    divide-by-three rule that already tracks English. The wide-character count
    comes from the UTF-8 byte length instead of a per-character scan because
    this runs several times per turn and the byte arithmetic is ~17x faster.
    It equals a per-character classification for single-script text (ASCII,
    CJK, Cyrillic, accented Latin, emoji) and errs high — reserving more — on
    mixed scripts.
    """
    n_chars = len(text)
    wide = min(n_chars, len(text.encode("utf-8")) - n_chars)
    return max(1, (n_chars - wide) // 3 + wide)


_REQUEST_MESSAGE_FIELDS = frozenset({
    "role", "content", "reasoning_content", "tool_calls", "tool_call_id", "name",
})
_REQUEST_ESTIMATE_MESSAGE_FIELDS = _REQUEST_MESSAGE_FIELDS | {"provider_state"}
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


def estimate_request_tokens(
    messages: list[dict], tools: list[dict] | None = None
) -> int:
    """Estimate the provider request input tokens a call will actually spend.

    Same per-character estimate as ``estimate_messages_tokens``, over a wider
    field set: provider state is included because Anthropic replays its native
    content blocks — cached thinking blocks among them — back as request input,
    so they are real input tokens rather than local bookkeeping.

    The request, message, and tool framing allowances cover provider envelopes
    and normalization tokens outside the serialized source payload. Charging
    one token per serialized UTF-8 byte used to bound the estimate from above;
    that bound overshot by 3x and stopped sessions holding most of their
    budget. With it gone these allowances are the only safety margin left
    (~19% over the bare estimate on realistic histories), so do not trim them
    without replacing the margin.
    """
    total = _REQUEST_PROTOCOL_TOKEN_OVERHEAD
    for message in messages:
        payload = {
            key: ("" if key == "content" and value is None else value)
            for key, value in message.items()
            if key in _REQUEST_ESTIMATE_MESSAGE_FIELDS
        }
        total += _MESSAGE_PROTOCOL_TOKEN_OVERHEAD + _serialized_tokens(payload)
    if tools:
        total += (
            _TOOLS_PROTOCOL_TOKEN_OVERHEAD
            + len(tools) * _TOOL_PROTOCOL_TOKEN_OVERHEAD
            + _serialized_tokens(tools)
        )
    return total
