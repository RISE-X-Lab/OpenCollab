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
_REQUEST_ESTIMATE_MESSAGE_FIELDS = _REQUEST_MESSAGE_FIELDS | {"provider_state", "response_items"}
# Provider history policies can omit recorded reasoning. ``provider_state``
# remains available because Anthropic replays native thinking blocks as input.
_REQUEST_ESTIMATE_MESSAGE_FIELDS_NO_REASONING = (
    _REQUEST_ESTIMATE_MESSAGE_FIELDS - {"reasoning_content"}
)
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


def _request_message_payload(
    message: dict,
    fields: frozenset[str],
    *,
    prefer_response_items: bool,
) -> dict:
    """Select recorded fields or the native Responses projection for a role."""
    role = message.get("role")
    if prefer_response_items and role == "system":
        return {"role": role, "content": message.get("content") or ""}
    if prefer_response_items and role == "tool":
        return {
            "role": role,
            "tool_call_id": message.get("tool_call_id"),
            "content": message.get("content") or "",
        }
    if prefer_response_items and message.get("response_items") is not None:
        return {"response_items": message["response_items"]}
    return {
        key: ("" if key == "content" and value is None else value)
        for key, value in message.items()
        if key in fields
    }


def estimate_messages_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    prefer_response_items: bool = False,
) -> int:
    """Estimate OpenAI-compatible request tokens, including structured payloads.

    Usage fallbacks and history compaction receive assistant tool calls, tool
    response IDs, and thinking traces in addition to ordinary text. Estimate
    the same protocol fields that the provider request normalizer transmits,
    then include registered tool schemas when they are part of the request.
    """
    total = 0
    for message in messages:
        payload = _request_message_payload(
            message,
            _REQUEST_MESSAGE_FIELDS,
            prefer_response_items=prefer_response_items,
        )
        total += _MESSAGE_TOKEN_OVERHEAD + _serialized_tokens(payload)
    if tools:
        total += _TOOLS_TOKEN_OVERHEAD + _serialized_tokens(tools)
    return total


def estimate_request_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    keep_reasoning_content: bool = True,
    prefer_response_items: bool = False,
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

    ``keep_reasoning_content`` follows the selected provider history policy.
    Callers with an already-normalized request can use the default field set.
    ``prefer_response_items`` selects the native replay shape used by the
    Responses protocol, where provider items replace legacy message fields.
    """
    total = _REQUEST_PROTOCOL_TOKEN_OVERHEAD + estimate_request_message_tokens(
        messages,
        keep_reasoning_content=keep_reasoning_content,
        prefer_response_items=prefer_response_items,
    )
    if tools:
        total += (
            _TOOLS_PROTOCOL_TOKEN_OVERHEAD
            + len(tools) * _TOOL_PROTOCOL_TOKEN_OVERHEAD
            + _serialized_tokens(tools)
        )
    return total


def estimate_request_message_tokens(
    messages: list[dict],
    *,
    keep_reasoning_content: bool = True,
    prefer_response_items: bool = False,
) -> int:
    """Estimate the per-message half of a request, without request framing.

    Same fields and per-message framing as :func:`estimate_request_tokens`,
    minus the once-per-request allowance, which is a constant and therefore
    not additive: history compaction re-estimates incrementally and needs the
    estimate of a message group plus the estimate of the remainder to equal
    the estimate of the whole.

    ``keep_reasoning_content`` selects the same field set the outbound request
    will carry; see :func:`estimate_request_tokens`.
    ``prefer_response_items`` is reserved for requests sent through the native
    Responses protocol. Generic history sizing leaves it false so recorded
    content and native state are both accounted for.
    """
    fields = (
        _REQUEST_ESTIMATE_MESSAGE_FIELDS
        if keep_reasoning_content
        else _REQUEST_ESTIMATE_MESSAGE_FIELDS_NO_REASONING
    )
    total = 0
    for message in messages:
        # Generic history sizing uses every recorded field. Provider-specific
        # reservation opts into the native Responses projection, whose role
        # precedence is shared with estimate_messages_tokens above.
        payload = _request_message_payload(
            message,
            fields,
            prefer_response_items=prefer_response_items,
        )
        total += _MESSAGE_PROTOCOL_TOKEN_OVERHEAD + _serialized_tokens(payload)
    return total
