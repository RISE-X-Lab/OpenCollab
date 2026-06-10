"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.llm.retry import with_retry
from opencollab.adapters.llm.types import LLMResponse, Usage


def _build_request_kwargs(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return kwargs


def _parse_response(resp: Any) -> LLMResponse:
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

    return LLMResponse(
        content=message.content,
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        ),
        finish_reason=choice.finish_reason,
    )


async def complete_openai(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_retries: int,
) -> LLMResponse:
    """Single-shot completion against an OpenAI-compatible endpoint."""
    kwargs = _build_request_kwargs(model, messages, tools, temperature)
    resp = await with_retry(
        lambda: client.chat.completions.create(**kwargs),
        max_retries=max_retries,
    )
    return _parse_response(resp)
