"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

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
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
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

    usage = _parse_usage(resp, request_messages, message)
    return LLMResponse(
        content=message.content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.finish_reason,
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
) -> LLMResponse:
    """Single-shot completion against an OpenAI-compatible endpoint."""
    kwargs = _build_request_kwargs(
        model, messages, tools, temperature, thinking, thinking_params
    )
    resp = await with_retry(
        lambda: client.chat.completions.create(**kwargs),
        max_retries=max_retries,
    )
    return _parse_response(resp, messages)
