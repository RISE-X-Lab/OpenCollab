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

# Kimi may return special-token tool markup in ``content`` or
# ``reasoning_content`` with ``finish_reason=stop`` and no parsed tool calls.
# Recover it so tools run.
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

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)

def _unquoted_markup_text(content: str) -> str:
    """Mask Markdown examples while retaining the original text offsets."""
    fence = ""
    lines = []
    for line in content.splitlines(keepends=True):
        marker = _FENCE_RE.match(line.rstrip("\r\n"))
        if fence:
            lines.append(" " * len(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
            lines.append(" " * len(line))
        elif line.startswith(("    ", "\t")) or line.lstrip().startswith(">"):
            lines.append(" " * len(line))
        else:
            lines.append(line)
    return _CODE_SPAN_RE.sub(lambda match: " " * len(match[0]), "".join(lines))

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

    searchable = _unquoted_markup_text(content)
    if (
        searchable.count(_MARKUP_SECTION_BEGIN) != 1
        or searchable.count(_MARKUP_SECTION_END) != 1
    ):
        return [], content
    start = searchable.index(_MARKUP_SECTION_BEGIN)
    section_start = start + len(_MARKUP_SECTION_BEGIN)
    end_idx = searchable.find(_MARKUP_SECTION_END, section_start)
    if end_idx < 0:
        return [], content
    # Markdown masking only decides where an executable section is located.
    # Tool JSON can legitimately contain indentation and shell backticks.
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
    """Build a response from a completion or reassembled stream.

    Shares markup recovery, usage estimation, and empty-turn rescue.
    ``usage_source`` supplies ``.usage``. ``usage_message`` is the assistant SDK
    object or dict used to estimate output tokens when usage is absent.
    """
    use_refusal = not (content or "").strip() and isinstance(refusal, str) and bool(refusal.strip())

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
    # Record recovered tool markup in run metrics.
    usage.markup_recovered = 1 if markup_recovered else 0
    # Thinking providers (e.g. kimi-k2.6 with ``enable_thinking``) put the
    # chain-of-thought in ``reasoning_content`` and the answer in ``content``.
    # Keep the reasoning for trajectory observability; the shared rescue rung
    # falls back to it only when the turn is otherwise empty.
    content_from_reasoning = False
    if finish_reason != "content_filter":
        content, content_from_reasoning = rescue_empty_turn(content, tool_calls, reasoning)
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=reasoning,
        provider_model=provider_model,
        content_from_reasoning=content_from_reasoning,
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
        plain_message = to_plain_data(message)
        # An empty completed turn can legitimately report zero output. Keep
        # that exact counter when the input and total agree, while retaining
        # estimates for absent or inconsistent usage and any generated output.
        reported_empty_turn = (
            not estimated
            and _optional_usage_int(raw_usage, "completion_tokens") == 0
            and _optional_usage_int(raw_usage, "total_tokens") == input_tokens
            and not reasoning_tokens
            and isinstance(plain_message, dict)
            and not any(plain_message.get(key) for key in (
                "content", "refusal", "reasoning_content", "reasoning", "tool_calls", "function_call", "audio",
            ))
        )
        if not reported_empty_turn:
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
