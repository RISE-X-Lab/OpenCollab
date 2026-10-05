"""Shared Chat response parsing, usage accounting and markup recovery."""

from __future__ import annotations

import json
import re
from typing import Any

from opencollab.adapters.llm._attempt_usage import _optional_usage_int
from opencollab.adapters.llm.types import (
    LLMResponse,
    Usage,
    estimate_messages_tokens,
    rescue_empty_turn,
    to_plain_data,
    usage_to_dict,
)

# kimi (DashScope OpenAI-compat) sometimes emits tool calls as literal text in
# ``message.content`` using these special-token delimiters, with
# finish_reason='stop' and an EMPTY parsed ``tool_calls`` list. Parse the markup
# back into a normal tool-call response so the intended tool actually runs.
_MARKUP_SECTION_BEGIN = "<|tool_calls_section_begin|>"
_MARKUP_SECTION_END = "<|tool_calls_section_end|>"
_MARKUP_CALL_BEGIN = "<|tool_call_begin|>"
_MARKUP_CALL_END = "<|tool_call_end|>"
_MARKUP_ARG_BEGIN = "<|tool_call_argument_begin|>"

# One tool-call block: header (functions.NAME:ID) then JSON args, between the
# call-begin and call-end markers. Non-greedy so multiple blocks parse cleanly.
_MARKUP_CALL_RE = re.compile(
    re.escape(_MARKUP_CALL_BEGIN)
    + r"\s*functions\.(?P<name>[^:\s]+):(?P<id>\S+?)\s*"
    + re.escape(_MARKUP_ARG_BEGIN)
    + r"(?P<args>.*?)"
    + re.escape(_MARKUP_CALL_END),
    re.DOTALL,
)


def _extract_markup_tool_calls(
    content: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Parse kimi's literal tool-call markup out of ``content``.

    Returns ``(tool_calls, cleaned_content)``. ``tool_calls`` uses the same dict
    shape this module builds from ``message.tool_calls``. ``cleaned_content`` is
    the surrounding prose with the markup section removed (``None`` if nothing
    meaningful remains). On any structural problem returns ``([], content)`` so
    the caller keeps its current behaviour.
    """
    if not content or _MARKUP_SECTION_BEGIN not in content:
        return [], content

    if (
        content.count(_MARKUP_SECTION_BEGIN) != 1
        or content.count(_MARKUP_SECTION_END) != 1
    ):
        return [], content
    start = content.index(_MARKUP_SECTION_BEGIN)
    section_start = start + len(_MARKUP_SECTION_BEGIN)
    end_idx = content.find(_MARKUP_SECTION_END, section_start)
    if end_idx < 0:
        return [], content
    section = content[section_start:end_idx]

    tool_calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = 0
    for match in _MARKUP_CALL_RE.finditer(section):
        if section[cursor:match.start()].strip():
            return [], content
        raw_args = match.group("args").strip()
        try:
            json.loads(raw_args)
        except (ValueError, TypeError):
            return [], content
        call_id = match.group("id")
        if call_id in seen_ids:
            return [], content
        seen_ids.add(call_id)
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": match.group("name"),
                "arguments": raw_args,
            },
        })
        cursor = match.end()

    if not tool_calls or section[cursor:].strip():
        return [], content

    # Strip the validated markup section while preserving surrounding prose.
    cleaned = content[:start] + content[end_idx + len(_MARKUP_SECTION_END):]
    cleaned = cleaned.strip()
    return tool_calls, (cleaned or None)


def _normalize_tool_arguments(arguments: str | None) -> str:
    raw = (arguments or "").strip()
    if raw.startswith("{}{"):
        candidate = raw[2:].strip()
        try:
            json.loads(candidate)
        except (TypeError, ValueError):
            return raw
        return candidate
    return raw


def _clean_provider_model(value: Any) -> str | None:
    """The provider-reported model id, or ``None`` when absent/blank."""
    return value if isinstance(value, str) and value else None


def _build_chat_response(
    content: str | None,
    reasoning: str | None,
    tool_calls: list[dict[str, Any]],
    finish_reason: str | None,
    provider_model: str | None,
    *,
    usage_source: Any,
    usage_message: Any,
    request_messages: list[dict],
    tools: list[dict] | None,
    refusal: str | None = None,
) -> LLMResponse:
    """Turn already-extracted response fields into an ``LLMResponse``.

    Both wire shapes (one non-streaming ``ChatCompletion`` and a reassembled
    chunk stream) end here, so the three contracts that live in this tail —
    kimi markup recovery, usage parsing with its estimate fallback, and the
    empty-turn rescue — apply to both by construction rather than by being
    hand-mirrored on each path.

    ``usage_source`` only needs a ``.usage`` attribute; ``usage_message`` is the
    assistant message (SDK object or plain dict) used to estimate output tokens
    when the endpoint reports none.
    """
    use_refusal = not (content or "").strip() and isinstance(refusal, str) and bool(refusal.strip())

    # kimi (DashScope compat) sometimes emits tool calls as literal special-token
    # markup instead of structured ``tool_calls`` — in ``content`` or, under
    # thinking mode, inside ``reasoning_content`` (finish_reason='stop', empty
    # ``message.tool_calls``). Recover them so the tool actually runs instead of
    # being treated as a prose stop.
    markup_recovered = False
    if not tool_calls and not use_refusal:
        markup_calls, cleaned = _extract_markup_tool_calls(content)
        if markup_calls:
            tool_calls = markup_calls
            content = cleaned
            markup_recovered = True
        elif reasoning:
            markup_calls, cleaned_reasoning = _extract_markup_tool_calls(reasoning)
            if markup_calls:
                tool_calls = markup_calls
                reasoning = cleaned_reasoning
                markup_recovered = True

    # Refusal fallback is display text, including any quoted tool protocol markers.
    if use_refusal:
        content = refusal

    usage = _parse_usage(usage_source, request_messages, usage_message, tools)
    # Surface the P6 recovery as an observability counter (summed up the chain
    # into the run metrics) without altering the recovered response itself.
    usage.markup_recovered = 1 if markup_recovered else 0
    # Thinking providers (e.g. kimi-k2.6 with ``enable_thinking``) put the
    # chain-of-thought in ``reasoning_content`` and the answer in ``content``.
    # Keep the reasoning for trajectory observability; the shared rescue rung
    # falls back to it only when the turn is otherwise empty.
    content = rescue_empty_turn(content, tool_calls, reasoning)
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=reasoning,
        provider_model=provider_model,
    )


def _parse_response(
    resp: Any, request_messages: list[dict], tools: list[dict] | None = None
) -> LLMResponse:
    choice = resp.choices[0]
    message = choice.message

    tool_calls = []
    if message.tool_calls:
        for tool_call in message.tool_calls:
            tool_calls.append({
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": _normalize_tool_arguments(tool_call.function.arguments),
                },
            })

    reasoning = getattr(message, "reasoning_content", None)
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = getattr(message, "reasoning", None)
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = None

    usage_message = message
    if reasoning is not None:
        usage_message = {**to_plain_data(message), "reasoning_content": reasoning}

    return _build_chat_response(
        message.content,
        reasoning,
        tool_calls,
        choice.finish_reason,
        _clean_provider_model(getattr(resp, "model", None)),
        usage_source=resp,
        usage_message=usage_message,
        request_messages=request_messages,
        tools=tools,
        refusal=getattr(message, "refusal", None),
    )


def _parse_usage(
    resp: Any,
    request_messages: list[dict],
    message: Any,
    tools: list[dict] | None = None,
) -> Usage:
    """Build a ``Usage`` from an OpenAI-compatible response, with estimate fallback.

    Some OpenAI-compatible endpoints (proxies, certain streaming configs,
    vLLM/Ollama) omit the ``usage`` block or report zero token counts. Left
    untreated the call would contribute 0 to the budget meter, so the budget
    would never trip and only ``max_steps`` would bound the session. When the
    reported counts are missing or zero we fall back to a non-zero estimate
    derived from the request messages (input) and response text (output).

    Note: OpenAI-compatible ``prompt_tokens`` ALREADY includes cached tokens
    (``cached_tokens`` appears only as a sub-detail under
    ``prompt_tokens_details``), so we do NOT add any cache field here — that
    would double-count. The additive cache fix applies only to Anthropic.
    """
    usage = getattr(resp, "usage", None)
    raw_usage = usage_to_dict(usage)
    input_tokens = _usage_int(raw_usage, "prompt_tokens")
    output_tokens = _usage_int(raw_usage, "completion_tokens")
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    cached_tokens = _usage_int(prompt_details, "cached_tokens")
    completion_details = raw_usage.get("completion_tokens_details") or {}
    reasoning_tokens = _usage_int(completion_details, "reasoning_tokens")

    estimated = False
    if input_tokens <= 0:
        input_tokens = estimate_messages_tokens(request_messages, tools)
        estimated = True
    if output_tokens <= 0:
        output_tokens = _estimate_output_tokens(message)
        estimated = True

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens or None,
        estimated=estimated,
        raw_usage=raw_usage,
    )


def _reported_chat_usage(usage: Any) -> Usage | None:
    """Retain reported counters from an interrupted attempt without estimating."""
    raw = usage_to_dict(usage)
    input_tokens = _optional_usage_int(raw, "prompt_tokens")
    output_tokens = _optional_usage_int(raw, "completion_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return Usage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        cache_read_tokens=_optional_usage_int(raw.get("prompt_tokens_details"), "cached_tokens"),
        reasoning_tokens=_optional_usage_int(raw.get("completion_tokens_details"), "reasoning_tokens"),
        estimated=input_tokens is None or output_tokens is None,
        raw_usage=raw,
    )


def _usage_int(source: Any, key: str) -> int:
    if not isinstance(source, dict):
        return 0
    value = source.get(key)
    if value in (None, ""):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _estimate_output_tokens(message: Any) -> int:
    """Estimate visible reply text and the supported structured output fields."""
    plain_message = to_plain_data(message)
    if not isinstance(plain_message, dict):
        return 0
    content, refusal = plain_message.get("content"), plain_message.get("refusal")
    if (
        (content is None or isinstance(content, str) and not content.strip())
        and isinstance(refusal, str) and refusal.strip()
    ):
        plain_message = {**plain_message, "content": refusal}
    return estimate_messages_tokens([{"role": "assistant", **plain_message}])
