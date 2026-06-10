"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from opencollab.adapters.llm.retry import with_retry
from opencollab.adapters.llm.types import LLMResponse, StreamDelta, Usage


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
        raw=resp,
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


async def stream_openai(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
) -> AsyncIterator[StreamDelta]:
    """Streaming completion against an OpenAI-compatible endpoint."""
    kwargs = _build_request_kwargs(model, messages, tools, temperature)
    kwargs["stream"] = True

    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        finish = chunk.choices[0].finish_reason

        if delta.content:
            yield StreamDelta(content=delta.content)

        if delta.tool_calls:
            for tool_call in delta.tool_calls:
                function = tool_call.function
                yield StreamDelta(
                    tool_call_index=tool_call.index,
                    tool_call_id=tool_call.id,
                    tool_call_name=function.name if function and function.name else None,
                    tool_call_args_delta=function.arguments if function else None,
                )

        if finish:
            yield StreamDelta(finish_reason=finish)
