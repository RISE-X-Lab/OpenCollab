"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

import json
import re
from typing import Any

from opencollab.adapters.llm.retry import with_retry
from opencollab.adapters.llm.types import (
    LLMResponse,
    Usage,
    estimate_messages_tokens,
    estimate_tokens,
)


def _build_request_kwargs(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    thinking: bool = False,
    thinking_params: dict | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        # Default "auto"; a caller may force "required" (forced-write step).
        kwargs["tool_choice"] = tool_choice or "auto"
    # Thinking passthrough: when on, the provider-specific reasoning params ride
    # along as ``extra_body`` (a valid OpenAI SDK create() kwarg) — for DashScope
    # compatible mode this is ``{"enable_thinking": True}``. When off, nothing is
    # added so the request is byte-for-byte unchanged. Merge into any existing
    # extra_body rather than clobbering it.
    if thinking and thinking_params:
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.update(thinking_params)
        kwargs["extra_body"] = extra_body
    return kwargs


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

    tool_calls: list[dict[str, Any]] = []
    for match in _MARKUP_CALL_RE.finditer(content):
        raw_args = match.group("args").strip()
        try:
            json.loads(raw_args)
        except (ValueError, TypeError):
            continue  # not valid JSON args -> skip this malformed block
        tool_calls.append({
            "id": match.group("id"),
            "type": "function",
            "function": {
                "name": match.group("name"),
                "arguments": raw_args,
            },
        })

    if not tool_calls:
        return [], content  # no well-formed block found -> fall back

    # Strip the whole markup section (begin..end inclusive) from the prose. The
    # end marker may be absent on a truncated stream; strip from begin onward.
    start = content.index(_MARKUP_SECTION_BEGIN)
    end_idx = content.find(_MARKUP_SECTION_END)
    if end_idx == -1:
        cleaned = content[:start]
    else:
        cleaned = content[:start] + content[end_idx + len(_MARKUP_SECTION_END):]
    cleaned = cleaned.strip()
    return tool_calls, (cleaned or None)


def _parse_response(resp: Any, request_messages: list[dict]) -> LLMResponse:
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
                    "arguments": tool_call.function.arguments,
                },
            })

    content = message.content
    reasoning = getattr(message, "reasoning_content", None) or None
    # kimi (DashScope compat) sometimes emits tool calls as literal special-token
    # markup instead of structured ``tool_calls`` — in ``content`` or, under
    # thinking mode, inside ``reasoning_content`` (finish_reason='stop', empty
    # ``message.tool_calls``). Recover them so the tool actually runs instead of
    # being treated as a prose stop.
    if not tool_calls:
        markup_calls, cleaned = _extract_markup_tool_calls(content)
        if markup_calls:
            tool_calls = markup_calls
            content = cleaned
        elif reasoning:
            markup_calls, cleaned_reasoning = _extract_markup_tool_calls(reasoning)
            if markup_calls:
                tool_calls = markup_calls
                reasoning = cleaned_reasoning

    usage = _parse_usage(resp, request_messages, message)
    # Thinking providers (e.g. kimi-k2.6 with ``enable_thinking``) put the
    # chain-of-thought in ``reasoning_content`` and the answer in ``content``.
    # Keep the reasoning for trajectory observability. Belt-and-suspenders: if a
    # turn ever returns content=None with neither an answer nor a tool call,
    # fall back to the reasoning rather than emit a silent empty-stop turn.
    if not content and not tool_calls:
        content = reasoning
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.finish_reason,
        reasoning=reasoning,
    )


def _parse_usage(resp: Any, request_messages: list[dict], message: Any) -> Usage:
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
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0 if usage else 0

    estimated = False
    if input_tokens <= 0:
        input_tokens = estimate_messages_tokens(request_messages)
        estimated = True
    if output_tokens <= 0:
        output_tokens = _estimate_output_tokens(message)
        estimated = True

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated=estimated,
    )


def _estimate_output_tokens(message: Any) -> int:
    """Estimate output tokens from response text + serialized tool-call args."""
    text = message.content or ""
    for tool_call in message.tool_calls or []:
        text += tool_call.function.name + tool_call.function.arguments
    return estimate_tokens(text) if text else 0


async def complete_openai(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_retries: int,
    thinking: bool = False,
    thinking_params: dict | None = None,
    tool_choice: str | None = None,
) -> LLMResponse:
    """Single-shot completion against an OpenAI-compatible endpoint."""
    kwargs = _build_request_kwargs(
        model, messages, tools, temperature, thinking, thinking_params, tool_choice
    )
    resp = await with_retry(
        lambda: client.chat.completions.create(**kwargs),
        max_retries=max_retries,
    )
    return _parse_response(resp, messages)
