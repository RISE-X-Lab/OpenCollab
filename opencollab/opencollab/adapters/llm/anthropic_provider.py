"""Anthropic request building, response parsing, and message conversion.

The session keeps history in OpenAI message format; this module converts it
to Anthropic's format on the way out.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.llm.retry import with_retry
from opencollab.adapters.llm.types import DEFAULT_MAX_OUTPUT_TOKENS, LLMResponse, Usage


def _build_request_kwargs(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    thinking: bool = False,
    thinking_params: dict | None = None,
) -> dict[str, Any]:
    system_parts, anthropic_messages = convert_to_anthropic_messages(messages)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "temperature": temperature,
    }
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)
    if tools:
        kwargs["tools"] = [_convert_tool(tool) for tool in tools]
    # Thinking passthrough (minimal, flag-guarded). This native-Anthropic path is
    # unused here (DashScope compatible mode runs the OpenAI path), so keep it
    # behind the flag: default-off changes nothing. ``thinking_params`` is the
    # OpenAI-shaped payload; the Anthropic API instead wants an extended-thinking
    # block, so we send a conservative enabled block rather than the raw params.
    if thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2048}
    return kwargs


def _convert_tool(tool: dict) -> dict:
    """Convert an OpenAI function-tool definition to Anthropic's schema."""
    func = tool["function"]
    return {
        "name": func["name"],
        "description": func.get("description", ""),
        "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
    }


async def complete_anthropic(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_retries: int,
    thinking: bool = False,
    thinking_params: dict | None = None,
) -> LLMResponse:
    """Single-shot completion against the Anthropic API."""
    kwargs = _build_request_kwargs(
        model, messages, tools, temperature, thinking, thinking_params
    )
    resp = await with_retry(
        lambda: client.messages.create(**kwargs),
        max_retries=max_retries,
    )

    content = ""
    tool_calls = []
    for block in resp.content:
        if block.type == "text":
            content += block.text
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {"name": block.name, "arguments": json.dumps(block.input)},
            })

    return LLMResponse(
        content=content or None,
        tool_calls=tool_calls,
        usage=_parse_usage(resp.usage),
        finish_reason=resp.stop_reason,
    )


def _parse_usage(usage: Any) -> Usage:
    """Build a corrected ``Usage`` from an Anthropic usage object.

    When prompt caching is active the Messages API splits input into three
    fields: ``input_tokens`` (uncached input newly processed),
    ``cache_read_input_tokens`` (read from cache), and
    ``cache_creation_input_tokens`` (written to cache). ``input_tokens`` does
    NOT include the cached portions, so the true input the model processed is
    the sum of all three. We fold them into the accounted ``input_tokens`` so
    the token budget can't run slow. Cache attributes are read defensively
    since the SDK object may not carry them when caching is off.
    """
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    uncached_input = getattr(usage, "input_tokens", 0) or 0
    return Usage(
        input_tokens=uncached_input + cache_read + cache_creation,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


# ---------------------------------------------------------------------------
# OpenAI-format → Anthropic-format message conversion
# ---------------------------------------------------------------------------


def convert_to_anthropic_messages(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Convert OpenAI-format message history to Anthropic format.

    Returns (system_parts, anthropic_messages).

    Key conversions:
    - role="system" → extracted to system_parts (Anthropic uses top-level system param)
    - role="assistant" with tool_calls → assistant with tool_use content blocks
    - role="tool" → role="user" with tool_result content blocks (merged if consecutive)
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []

    for message in messages:
        role = message.get("role", "")

        if role == "system":
            system_parts.append(message.get("content", ""))
        elif role == "user":
            anthropic_messages.append({"role": "user", "content": message.get("content", "")})
        elif role == "assistant":
            content_blocks = _convert_assistant_content(message)
            if content_blocks:
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
        elif role == "tool":
            _append_tool_result(anthropic_messages, message)

    return system_parts, anthropic_messages


def _convert_assistant_content(message: dict) -> list[dict]:
    """Build Anthropic content blocks (text + tool_use) from an assistant message."""
    content_blocks: list[dict] = []
    if message.get("content"):
        content_blocks.append({"type": "text", "text": message["content"]})
    for tool_call in message.get("tool_calls") or []:
        content_blocks.append(_convert_tool_call(tool_call))
    return content_blocks


def _convert_tool_call(tool_call: dict) -> dict:
    """Convert one OpenAI tool call to an Anthropic tool_use block."""
    func = tool_call["function"]
    try:
        arguments = func["arguments"]
        tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        tool_input = {}
    return {
        "type": "tool_use",
        "id": tool_call["id"],
        "name": func["name"],
        "input": tool_input,
    }


def _append_tool_result(anthropic_messages: list[dict], message: dict) -> None:
    """Append a tool result, merging consecutive results into one user message."""
    tool_result_block = {
        "type": "tool_result",
        "tool_use_id": message.get("tool_call_id", ""),
        "content": message.get("content", ""),
    }
    last = anthropic_messages[-1] if anthropic_messages else None
    if last is not None and last["role"] == "user" and isinstance(last["content"], list):
        last["content"].append(tool_result_block)
    else:
        anthropic_messages.append({"role": "user", "content": [tool_result_block]})
