"""Anthropic request building, response parsing, and message conversion.

The session keeps history in OpenAI message format; this module converts it
to Anthropic's format on the way out.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from opencollab.adapters.llm.retry import with_retry
from opencollab.adapters.llm.types import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMResponse,
    Usage,
    rescue_empty_turn,
)


def _anthropic_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    """Map OpenAI-style ``tool_choice`` values to Anthropic's dict form.

    Anthropic expects ``{"type": "auto"|"any"|"tool"}``; OpenAI's ``"required"``
    (force *some* tool) maps to ``"any"``. A named OpenAI function choice maps
    to ``{"type": "tool", "name": ...}``. ``None`` keeps the API default.
    """
    if tool_choice is None:
        return None
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "auto":
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function") or {}
            name = function.get("name")
            if name:
                return {"type": "tool", "name": name}
        if tool_choice.get("type") == "tool" and tool_choice.get("name"):
            return {"type": "tool", "name": tool_choice["name"]}
    return None


def _anthropic_thinking_kwargs(
    thinking_params: dict | None,
    *,
    max_tokens: int,
) -> dict[str, Any]:
    """Validate provider-native thinking settings for one Anthropic request."""
    if not isinstance(thinking_params, dict) or not thinking_params:
        raise ValueError(
            "Anthropic thinking requires provider-native thinking_params"
        )

    unknown = set(thinking_params) - {"thinking", "output_config"}
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise ValueError(f"unsupported Anthropic thinking parameter(s): {names}")

    thinking_config = thinking_params.get("thinking")
    if not isinstance(thinking_config, dict):
        raise ValueError("Anthropic thinking_params.thinking must be an object")
    thinking_type = thinking_config.get("type")
    if not isinstance(thinking_type, str) or thinking_type not in {
        "enabled",
        "adaptive",
    }:
        raise ValueError(
            "Anthropic thinking_params.thinking.type must be enabled or adaptive"
        )
    allowed_thinking = (
        {"type", "budget_tokens", "display"}
        if thinking_type == "enabled"
        else {"type", "display"}
    )
    unknown_thinking = set(thinking_config) - allowed_thinking
    if unknown_thinking:
        names = ", ".join(sorted(map(str, unknown_thinking)))
        raise ValueError(f"unsupported Anthropic thinking field(s): {names}")
    display = thinking_config.get("display")
    if display is not None and (
        not isinstance(display, str) or display not in {"summarized", "omitted"}
    ):
        raise ValueError("Anthropic thinking display must be summarized or omitted")

    if thinking_type == "enabled":
        budget = thinking_config.get("budget_tokens")
        if isinstance(budget, bool) or not isinstance(budget, int):
            raise ValueError(
                "Anthropic manual thinking requires an integer budget_tokens"
            )
        if budget < 1024:
            raise ValueError("Anthropic thinking budget_tokens must be at least 1024")
        if budget >= max_tokens:
            raise ValueError(
                "Anthropic thinking budget_tokens must be less than max_output_tokens"
            )
    request = {"thinking": copy.deepcopy(thinking_config)}
    if "output_config" in thinking_params:
        output_config = thinking_params["output_config"]
        if not isinstance(output_config, dict):
            raise ValueError("Anthropic thinking_params.output_config must be an object")
        unknown_output = set(output_config) - {"effort", "format"}
        if unknown_output:
            names = ", ".join(sorted(map(str, unknown_output)))
            raise ValueError(f"unsupported Anthropic output_config field(s): {names}")
        if "effort" in output_config:
            effort = output_config["effort"]
            if not isinstance(effort, str) or effort not in {
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            }:
                raise ValueError("unsupported Anthropic output_config.effort")
        if "format" in output_config:
            _validate_anthropic_output_format(output_config["format"])
        request["output_config"] = copy.deepcopy(output_config)
    return request


def _validate_anthropic_output_format(value: Any) -> None:
    """Validate the JSON-schema output format accepted by Anthropic."""
    if not isinstance(value, dict) or set(value) != {"type", "schema"}:
        raise ValueError("Anthropic output_config.format requires type and schema")
    if value["type"] != "json_schema" or not isinstance(value["schema"], dict):
        raise ValueError("Anthropic output_config.format must contain a JSON schema")


def _build_request_kwargs(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    thinking: bool = False,
    thinking_params: dict | None = None,
    tool_choice: Any = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    system_parts, anthropic_messages = convert_to_anthropic_messages(messages)
    max_tokens = int(max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
    thinking_kwargs = (
        _anthropic_thinking_kwargs(thinking_params, max_tokens=max_tokens)
        if thinking
        else {}
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
    }
    # Anthropic requires the default temperature while thinking is enabled.
    # Omitting it also works for compatible gateways and avoids turning the
    # ordinary OpenCollab temperature default into a provider-side 400.
    if not thinking:
        kwargs["temperature"] = temperature
    # Nucleus sampling rides along ONLY when explicitly set; when None the key is
    # omitted so the request is byte-for-byte identical to today's behavior.
    if thinking and top_p is not None:
        raise ValueError("Anthropic thinking requires the provider-default top_p")
    if top_p is not None:
        kwargs["top_p"] = top_p
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)
    if tools:
        kwargs["tools"] = [_convert_tool(tool) for tool in tools]
        choice = _anthropic_tool_choice(tool_choice)
        if choice is not None:
            if (
                thinking_kwargs.get("thinking", {}).get("type") == "enabled"
                and choice.get("type") in {"any", "tool"}
            ):
                choice = {"type": "auto"}
            kwargs["tool_choice"] = choice
    kwargs.update(thinking_kwargs)
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
    tool_choice: Any = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
) -> LLMResponse:
    """Single-shot completion against the Anthropic API."""
    kwargs = _build_request_kwargs(
        model,
        messages,
        tools,
        temperature,
        thinking,
        thinking_params,
        tool_choice,
        top_p,
        max_output_tokens,
    )
    resp = await with_retry(
        lambda: client.messages.create(**kwargs),
        max_retries=max_retries,
    )
    return _parse_response(resp)


def _parse_response(resp: Any) -> LLMResponse:
    content = ""
    thinking_text = ""
    provider_content: list[dict[str, Any]] = []
    has_thinking = False
    tool_calls = []
    for block in resp.content:
        provider_content.append(_serialize_content_block(block))
        if block.type == "text":
            content += _text_or_empty(getattr(block, "text", None))
        elif block.type == "thinking":
            thinking_text += _text_or_empty(getattr(block, "thinking", None))
            has_thinking = True
        elif block.type == "redacted_thinking":
            has_thinking = True
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {"name": block.name, "arguments": json.dumps(block.input)},
            })

    # Keep the thinking text for trajectory observability; the shared rescue
    # rung falls back to it only when the turn is otherwise empty.
    reasoning = thinking_text or None
    content = rescue_empty_turn(content, tool_calls, reasoning)

    return LLMResponse(
        content=content or None,
        tool_calls=tool_calls,
        usage=_parse_usage(resp.usage),
        finish_reason=resp.stop_reason,
        reasoning=reasoning,
        provider_state=(
            {"anthropic_content": provider_content}
            if has_thinking
            else None
        ),
    )


def _text_or_empty(value: Any) -> str:
    """Normalize nullable text fields emitted by compatible gateways."""
    return value if isinstance(value, str) else ""


def _serialize_content_block(block: Any) -> dict[str, Any]:
    """Return a JSON-safe Anthropic block for an unchanged later request."""
    if hasattr(block, "model_dump"):
        payload = block.model_dump(mode="json", exclude_none=True)
        if isinstance(payload, dict):
            if payload.get("type") == "text":
                payload["text"] = _text_or_empty(payload.get("text"))
            elif payload.get("type") == "thinking":
                payload["thinking"] = _text_or_empty(payload.get("thinking"))
            return payload
    if isinstance(block, dict):
        return copy.deepcopy(block)
    if block.type == "text":
        return {"type": "text", "text": _text_or_empty(getattr(block, "text", None))}
    if block.type == "thinking":
        payload = {
            "type": "thinking",
            "thinking": _text_or_empty(getattr(block, "thinking", None)),
        }
        signature = getattr(block, "signature", None)
        if signature is not None:
            payload["signature"] = signature
        return payload
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": getattr(block, "data", "")}
    return {
        "type": "tool_use",
        "id": block.id,
        "name": block.name,
        "input": copy.deepcopy(block.input),
    }


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
        raw_usage={
            "input_tokens": uncached_input,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
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

    for message_index, message in enumerate(messages):
        role = message.get("role", "")

        if role == "system":
            content = message.get("content", "")
            blocks = _normalize_openai_content(
                content,
                message_index=message_index,
                role=role,
            )
            if message.get("compacted"):
                # A compaction record is chronological conversation context, not a
                # global instruction. Keep it at its original position instead of
                # hoisting it into Anthropic's top-level system field.
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": content if isinstance(content, str) else blocks,
                    }
                )
            else:
                system_parts.append("".join(block["text"] for block in blocks))
        elif role == "user":
            content = message.get("content", "")
            normalized = (
                content
                if isinstance(content, str)
                else _normalize_openai_content(
                    content,
                    message_index=message_index,
                    role=role,
                )
            )
            anthropic_messages.append({"role": "user", "content": normalized})
        elif role == "assistant":
            content_blocks = _convert_assistant_content(message, message_index=message_index)
            if content_blocks:
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
        elif role == "tool":
            _append_tool_result(anthropic_messages, message)

    return system_parts, anthropic_messages


def _normalize_openai_content(
    content: Any,
    *,
    message_index: int,
    role: str,
) -> list[dict[str, str]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ValueError(
            f"message {message_index} ({role}) content must be text or a content block list"
        )

    normalized: list[dict[str, str]] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            raise ValueError(
                f"message {message_index} ({role}) content block {block_index} must be an object"
            )
        block_type = block.get("type")
        if block_type not in {"text", "input_text"}:
            raise ValueError(
                f"message {message_index} ({role}) unsupported content block type: {block_type}"
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError(
                f"message {message_index} ({role}) content block {block_index} requires text"
            )
        normalized.append({"type": "text", "text": text})
    return normalized


def _convert_assistant_content(message: dict, *, message_index: int) -> list[dict]:
    """Build Anthropic content blocks (text + tool_use) from an assistant message."""
    provider_state = message.get("provider_state")
    if isinstance(provider_state, dict):
        provider_content = provider_state.get("anthropic_content")
        if isinstance(provider_content, list):
            return copy.deepcopy(provider_content)
    content_blocks: list[dict] = _normalize_openai_content(
        message.get("content", ""),
        message_index=message_index,
        role="assistant",
    )
    for tool_call in message.get("tool_calls") or []:
        content_blocks.append(_convert_tool_call(tool_call, message_index=message_index))
    return content_blocks


def _convert_tool_call(tool_call: dict, *, message_index: int) -> dict:
    """Convert one OpenAI tool call to an Anthropic tool_use block."""
    func = tool_call["function"]
    name = func["name"]
    call_id = tool_call["id"]
    arguments = func.get("arguments")
    location = f"message {message_index} (assistant) tool {name} ({call_id})"
    if isinstance(arguments, str):
        try:
            tool_input = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{location} has invalid JSON arguments: {exc.msg}") from exc
    elif isinstance(arguments, dict):
        tool_input = copy.deepcopy(arguments)
    else:
        raise ValueError(f"{location} arguments must be JSON text or an object")
    if not isinstance(tool_input, dict):
        raise ValueError(f"{location} arguments must decode to a JSON object")
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
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
