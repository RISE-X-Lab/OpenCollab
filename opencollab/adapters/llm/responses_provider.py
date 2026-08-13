"""OpenAI Responses API request conversion and typed stream parsing."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from opencollab.adapters.llm.errors import TransientProviderError
from opencollab.adapters.llm.responses_errors import (
    ResponsesEmptyOutputError,
    ResponsesProtocolError,
    ResponsesStreamInterruptedError,
    ResponsesTerminalEventError,
    ResponsesTransientEventError,
)
from opencollab.adapters.llm.responses_structured import (
    ForcedTextTool,
    forced_text_format,
    forced_text_tool,
    project_forced_text_tool,
)
from opencollab.adapters.llm.responses_usage import parse_responses_usage
from opencollab.adapters.llm.retry import RetryTimeBudget, with_retry
from opencollab.adapters.llm.tool_contracts import (
    normalize_function_tools,
    normalize_text_content,
    normalize_tool_choice,
    validate_tool_choice_target,
)
from opencollab.adapters.llm.types import (
    LLMResponse,
    model_capabilities,
    rescue_empty_turn,
    to_plain_data,
)

_OUTPUT_ITEM_TYPES = frozenset({"message", "reasoning", "function_call"})
_PASSIVE_EVENT_TYPES = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.queued",
        "response.output_item.added",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_text.annotation.added",
        "response.refusal.delta",
        "response.refusal.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    }
)


@dataclass
class _StreamState:
    output_items: list[dict[str, Any]] = field(default_factory=list)
    argument_fragments: dict[int, list[str]] = field(default_factory=dict)
    completed_response: Any = None


def _message_text(content: Any) -> str:
    try:
        return normalize_text_content(content)
    except ValueError as exc:
        raise ResponsesProtocolError(str(exc)) from exc


def _message_content_parts(content: Any) -> str | list[dict[str, Any]]:
    """Project legacy text/image blocks into Responses input content."""
    if not isinstance(content, list):
        return _message_text(content)
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            raise ResponsesProtocolError("message content contains an unsupported block")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
            continue
        if kind == "image_url" and isinstance(part.get("image_url"), dict):
            image = part["image_url"]
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise ResponsesProtocolError("image_url block is missing url")
            item = {"type": "input_image", "image_url": url}
            if image.get("detail") is not None:
                item["detail"] = image["detail"]
            parts.append(item)
            continue
        raise ResponsesProtocolError(f"unsupported message content block {kind!r}")
    return parts


def _validated_response_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ResponsesProtocolError("response_items must be a list")
    items: list[dict[str, Any]] = []
    for raw in value:
        item = to_plain_data(raw)
        if not isinstance(item, dict) or item.get("type") not in _OUTPUT_ITEM_TYPES:
            raise ResponsesProtocolError("response_items contain an unsupported item")
        items.append(item)
    return items


def _normalized_arguments(value: Any) -> str:
    if not isinstance(value, str):
        raise ResponsesProtocolError("function call arguments must be JSON text")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ResponsesProtocolError("function call arguments contain invalid JSON") from exc
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _function_call_identity(call_id: Any, name: Any, arguments: Any) -> tuple[str, str, str]:
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ResponsesProtocolError("function call is missing call_id or name")
    return call_id, name, _normalized_arguments(arguments)


def _verify_replay_tool_calls(message: dict[str, Any], items: list[dict[str, Any]]) -> None:
    if "tool_calls" not in message:
        return
    legacy = []
    for call in message.get("tool_calls") or ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise ResponsesProtocolError("legacy tool call is missing function data")
        legacy.append(
            _function_call_identity(
                call.get("id"),
                function.get("name"),
                function.get("arguments") or "{}",
            )
        )
    replayed = [
        _function_call_identity(item.get("call_id"), item.get("name"), item.get("arguments"))
        for item in items
        if item["type"] == "function_call"
    ]
    if legacy != replayed:
        raise ResponsesProtocolError("tool_calls and response_items disagree")


def _messages_to_input(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    answered_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _message_text(message.get("content"))
            if text:
                if message.get("compacted"):
                    items.append({"role": "user", "content": text})
                else:
                    instructions.append(text)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ResponsesProtocolError("function_call_output is missing call_id")
            if call_id not in call_ids:
                raise ResponsesProtocolError(f"function_call_output has no matching call_id {call_id!r}")
            if call_id in answered_ids:
                raise ResponsesProtocolError(f"duplicate function_call_output for {call_id!r}")
            answered_ids.add(call_id)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _message_text(message.get("content")),
                }
            )
            continue
        replay = message.get("response_items")
        if replay is not None:
            replay_items = _validated_response_items(replay)
            _verify_replay_tool_calls(message, replay_items)
            for item in replay_items:
                if item["type"] != "function_call":
                    continue
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ResponsesProtocolError("replayed function_call is missing call_id")
                if call_id in call_ids:
                    raise ResponsesProtocolError(f"duplicate replayed call_id {call_id!r}")
                call_ids.add(call_id)
            items.extend(replay_items)
            continue
        if role not in {"user", "assistant"}:
            raise ResponsesProtocolError(f"unsupported message role {role!r}")
        content = _message_content_parts(message.get("content"))
        if content:
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls") or ():
            function = call.get("function") if isinstance(call, dict) else None
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(call_id, str) or not call_id:
                raise ResponsesProtocolError("legacy tool call is missing function data or call_id")
            if call_id in call_ids:
                raise ResponsesProtocolError(f"duplicate legacy call_id {call_id!r}")
            call_ids.add(call_id)
            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "{}",
                }
            )
    return ("\n\n".join(instructions) or None), items


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    try:
        normalized_tools = normalize_function_tools(tools)
    except ValueError as exc:
        raise ResponsesProtocolError(str(exc)) from exc
    converted: list[dict[str, Any]] = []
    for tool in normalized_tools:
        function = tool["function"]
        item = {
            "type": "function",
            "name": function["name"],
            "parameters": function["parameters"],
        }
        if function.get("description") is not None:
            item["description"] = function["description"]
        if function.get("strict") is not None:
            item["strict"] = function["strict"]
        converted.append(item)
    return converted


def _responses_tool_choice(
    value: Any,
    converted_tools: list[dict[str, Any]],
) -> Any:
    try:
        choice = normalize_tool_choice(value)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in converted_tools
        ]
        validate_tool_choice_target(choice, openai_tools)
    except ValueError as exc:
        raise ResponsesProtocolError(str(exc)) from exc
    if choice is None:
        return "auto"
    if choice.mode == "named":
        return {"type": "function", "name": choice.name}
    return choice.mode


def _forced_text_tool(
    model: str,
    converted_tools: list[dict[str, Any]],
    tool_choice: Any,
) -> ForcedTextTool | None:
    """Bind one named tool through ``text.format`` when forcing is unsupported."""
    choice = _responses_tool_choice(tool_choice, converted_tools)
    try:
        return forced_text_tool(
            converted_tools,
            choice,
            supports_forced_tool_choice=(model_capabilities(model).supports_forced_tool_choice),
            supports_json_schema=model_capabilities(model).supports_responses_json_schema,
        )
    except ValueError as exc:
        raise ResponsesProtocolError(str(exc)) from exc


def _build_request_kwargs(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    *,
    tool_choice: Any = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    prompt_cache_namespace: str | None = None,
    response_session_id: str | None = None,
) -> dict[str, Any]:
    instructions, input_items = _messages_to_input(messages)
    if not input_items:
        raise ResponsesProtocolError("Responses request has no input items")
    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": False,
        "stream": model_capabilities(model).supports_responses_streaming,
    }
    capabilities = model_capabilities(model)
    if capabilities.supports_responses_reasoning:
        kwargs["include"] = ["reasoning.encrypted_content"]
    if capabilities.supports_responses_sampling:
        kwargs["temperature"] = temperature
    elif top_p is not None:
        raise ResponsesProtocolError(f"model {model!r} does not support explicit top_p")
    if reasoning_effort is not None and not capabilities.supports_responses_reasoning:
        raise ResponsesProtocolError(f"model {model!r} does not support explicit reasoning_effort")
    if instructions:
        kwargs["instructions"] = instructions
    converted_tools = _responses_tools(tools)
    if converted_tools and not capabilities.supports_responses_tools:
        raise ResponsesProtocolError(f"model {model!r} does not support function tools")
    choice = _responses_tool_choice(tool_choice, converted_tools)
    if converted_tools:
        text_tool = _forced_text_tool(model, converted_tools, tool_choice)
        if text_tool is not None:
            kwargs["text"] = forced_text_format(text_tool)
        else:
            kwargs["tools"] = converted_tools
            if not model_capabilities(model).supports_forced_tool_choice and choice is not None and choice != "auto":
                choice = "auto"
            kwargs["tool_choice"] = choice
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = int(max_output_tokens)
    if reasoning_effort is not None:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    if prompt_cache_namespace and response_session_id:
        kwargs["prompt_cache_key"] = hashlib.sha256(
            f"{prompt_cache_namespace}\0{response_session_id}".encode()
        ).hexdigest()
    return kwargs


async def _next_event(iterator: Any, timeout: float, *, stage: str) -> Any:
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ResponsesProtocolError(f"Responses {stage} timeout after {timeout:g}s") from exc
    except StopAsyncIteration as exc:
        raise ResponsesStreamInterruptedError("Responses stream ended before response.completed") from exc


def _event_type(event: Any) -> str:
    value = getattr(event, "type", None)
    if not isinstance(value, str) or not value:
        raise ResponsesProtocolError("Responses event is missing its type")
    return value


def _event_error_data(event: Any) -> Any:
    error = to_plain_data(getattr(event, "error", None))
    if error is None and getattr(event, "type", None) == "error":
        plain_event = to_plain_data(event)
        if isinstance(plain_event, dict):
            error = {
                "code": plain_event.get("code"),
                "message": plain_event.get("message"),
            }
    if error is None:
        response = getattr(event, "response", None)
        error = to_plain_data(getattr(response, "error", None))
        if error is None:
            error = to_plain_data(getattr(response, "incomplete_details", None))
    return error


def _event_error(event: Any) -> str:
    error = _event_error_data(event)
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error.get("reason") or "unknown Responses error")
    return str(error or "unknown Responses error")


def _accept_output_item(event: Any, state: _StreamState) -> None:
    item = to_plain_data(getattr(event, "item", None))
    if not isinstance(item, dict) or item.get("type") not in _OUTPUT_ITEM_TYPES:
        raise ResponsesProtocolError("response.output_item.done carried an unsupported item")
    if item["type"] == "function_call":
        call_id = item.get("call_id")
        arguments = item.get("arguments")
        if not isinstance(call_id, str) or not call_id:
            raise ResponsesProtocolError("function_call is missing call_id")
        if any(
            prior.get("type") == "function_call" and prior.get("call_id") == call_id for prior in state.output_items
        ):
            raise ResponsesProtocolError(f"duplicate function_call call_id {call_id!r}")
        if not isinstance(arguments, str):
            raise ResponsesProtocolError(f"function_call {call_id!r} is missing arguments")
        index = getattr(event, "output_index", None)
        fragments = state.argument_fragments.pop(index, []) if isinstance(index, int) else []
        if fragments and "".join(fragments) != arguments:
            raise ResponsesProtocolError(f"function_call {call_id!r} argument fragments disagree")
        try:
            json.loads(arguments)
        except (TypeError, ValueError) as exc:
            raise ResponsesProtocolError(f"function_call {call_id!r} has invalid JSON") from exc
    state.output_items.append(item)


def _validate_terminal_response(
    response: Any,
    expected_model: str | None,
) -> tuple[str, str]:
    status = getattr(response, "status", None)
    if status not in {"completed", "incomplete"}:
        raise ResponsesProtocolError(f"Responses request ended with status {status!r}")
    error = to_plain_data(getattr(response, "error", None))
    if error is not None:
        raise ResponsesProtocolError(f"terminal Responses object contains error {error!r}")
    incomplete = to_plain_data(getattr(response, "incomplete_details", None))
    if status == "completed" and incomplete is not None:
        raise ResponsesProtocolError(f"completed Responses object contains incomplete details {incomplete!r}")
    finish_reason = "stop"
    if status == "incomplete":
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        if reason not in {"max_tokens", "max_output_tokens"}:
            raise ResponsesProtocolError(
                f"incomplete Responses object has unsupported reason {reason!r}"
            )
        finish_reason = "max_tokens"
    actual_model = getattr(response, "model", None)
    if not isinstance(actual_model, str) or not actual_model:
        raise ResponsesProtocolError("terminal Responses object is missing model identity")
    return actual_model, finish_reason


def _handle_event(event: Any, state: _StreamState, expected_model: str | None = None) -> bool:
    event_type = _event_type(event)
    if event_type in {"error", "response.failed"}:
        error = _event_error_data(event)
        message = _event_error(event)
        code = error.get("code") if isinstance(error, dict) else None
        if code in {"rate_limit_exceeded", "server_error", "vector_store_timeout"}:
            status_code = 429 if code == "rate_limit_exceeded" else 503
            raise ResponsesTransientEventError(
                message,
                code=code,
                status_code=status_code,
            )
        status_code = 400 if code == "context_length_exceeded" else None
        raise ResponsesTerminalEventError(
            message,
            code=code,
            status_code=status_code,
        )
    if event_type == "response.incomplete":
        response = getattr(event, "response", None)
        if getattr(response, "status", None) != "incomplete":
            raise ResponsesTerminalEventError(_event_error(event))
        state.completed_response = response
        _validate_terminal_response(response, expected_model)
        return True
    if event_type == "response.function_call_arguments.delta":
        index = getattr(event, "output_index", None)
        delta = getattr(event, "delta", None)
        if not isinstance(index, int) or not isinstance(delta, str):
            raise ResponsesProtocolError("tool argument delta is missing output_index or text")
        state.argument_fragments.setdefault(index, []).append(delta)
        return False
    if event_type == "response.function_call_arguments.done":
        index = getattr(event, "output_index", None)
        arguments = getattr(event, "arguments", None)
        if not isinstance(index, int) or not isinstance(arguments, str):
            raise ResponsesProtocolError("tool argument completion is incomplete")
        fragments = state.argument_fragments.get(index)
        if fragments and "".join(fragments) != arguments:
            raise ResponsesProtocolError("tool argument completion disagrees with its deltas")
        return False
    if event_type == "response.output_item.done":
        _accept_output_item(event, state)
        return False
    if event_type == "response.completed":
        state.completed_response = getattr(event, "response", None)
        if state.completed_response is None:
            raise ResponsesProtocolError("response.completed is missing the response object")
        _validate_terminal_response(state.completed_response, expected_model)
        return True
    if event_type not in _PASSIVE_EVENT_TYPES:
        raise ResponsesProtocolError(f"unsupported Responses event {event_type!r}")
    return False


async def _consume_stream(
    stream: Any,
    first_event_timeout: float,
    idle_timeout: float,
    expected_model: str | None = None,
) -> _StreamState:
    state = _StreamState()
    iterator = stream.__aiter__()
    first = True
    try:
        while True:
            event = await _next_event(
                iterator,
                first_event_timeout if first else idle_timeout,
                stage="first-event" if first else "stream-idle",
            )
            first = False
            if _handle_event(event, state, expected_model):
                break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
    if state.argument_fragments:
        raise ResponsesProtocolError("Responses stream ended with incomplete tool arguments")
    return state


async def _create_and_consume_stream(
    client: Any,
    kwargs: dict[str, Any],
    first_event_timeout: float,
    idle_timeout: float,
    expected_model: str,
) -> _StreamState:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + first_event_timeout
    try:
        event_stream = await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=first_event_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ResponsesProtocolError(f"Responses first-event timeout after {first_event_timeout:g}s") from exc

    remaining = deadline - loop.time()
    if remaining <= 0:
        close = getattr(event_stream, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        raise ResponsesProtocolError(f"Responses first-event timeout after {first_event_timeout:g}s")
    return await _consume_stream(
        event_stream,
        remaining,
        idle_timeout,
        expected_model,
    )


def _output_text(item: dict[str, Any]) -> str:
    if item.get("type") != "message":
        return ""
    parts: list[str] = []
    for content in item.get("content") or ():
        if not isinstance(content, dict):
            continue
        if content.get("type") == "output_text" and isinstance(content.get("text"), str):
            parts.append(content["text"])
        elif content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
            parts.append(content["refusal"])
    return "".join(parts)


def _reasoning_text(item: dict[str, Any]) -> str:
    if item.get("type") != "reasoning":
        return ""
    parts: list[str] = []
    for summary in item.get("summary") or ():
        if isinstance(summary, dict) and isinstance(summary.get("text"), str):
            parts.append(summary["text"])
    return "\n".join(parts)


def _semantic_output_item(item: dict[str, Any]) -> tuple[Any, ...]:
    """Return the stable meaning shared by streamed and terminal output items."""
    item_type = item.get("type")
    if item_type == "function_call":
        return (
            item_type,
            *_function_call_identity(
                item.get("call_id"),
                item.get("name"),
                item.get("arguments"),
            ),
        )
    if item_type == "message":
        role = item.get("role")
        if not isinstance(role, str) or not role:
            raise ResponsesProtocolError("message output item is missing its role")
        parts: list[tuple[str, str]] = []
        for raw_part in item.get("content") or ():
            if not isinstance(raw_part, dict):
                raise ResponsesProtocolError("message output contains an invalid content part")
            part_type = raw_part.get("type")
            if part_type == "output_text" and isinstance(raw_part.get("text"), str):
                parts.append((part_type, raw_part["text"]))
            elif part_type == "refusal" and isinstance(raw_part.get("refusal"), str):
                parts.append((part_type, raw_part["refusal"]))
            else:
                raise ResponsesProtocolError("message output contains an unsupported content part")
        return item_type, role, tuple(parts)
    if item_type == "reasoning":
        summaries: list[tuple[str | None, str]] = []
        for raw_summary in item.get("summary") or ():
            if not isinstance(raw_summary, dict) or not isinstance(raw_summary.get("text"), str):
                raise ResponsesProtocolError("reasoning output contains an invalid summary part")
            summaries.append((raw_summary.get("type"), raw_summary["text"]))
        return item_type, tuple(summaries)
    raise ResponsesProtocolError("response output contains an unsupported item")


def _output_items_agree(streamed: dict[str, Any], terminal: dict[str, Any]) -> bool:
    if _semantic_output_item(streamed) != _semantic_output_item(terminal):
        return False
    if streamed.get("type") != "reasoning":
        return True
    streamed_encrypted = streamed.get("encrypted_content")
    terminal_encrypted = terminal.get("encrypted_content")
    for value in (streamed_encrypted, terminal_encrypted):
        if value is not None and not isinstance(value, str):
            raise ResponsesProtocolError("reasoning output contains invalid encrypted content")
    return streamed_encrypted is None or terminal_encrypted is None or streamed_encrypted == terminal_encrypted


def _merge_terminal_projection(
    streamed: dict[str, Any],
    terminal: dict[str, Any],
) -> dict[str, Any]:
    if (
        streamed.get("type") == "reasoning"
        and streamed.get("encrypted_content") is None
        and terminal.get("encrypted_content") is not None
    ):
        return {**streamed, "encrypted_content": terminal["encrypted_content"]}
    return streamed


def _parse_stream(
    state: _StreamState,
    messages: list[dict[str, Any]],
    expected_model: str | None = None,
    forced_text_tool: ForcedTextTool | None = None,
) -> LLMResponse:
    actual_model, finish_reason = _validate_terminal_response(
        state.completed_response,
        expected_model,
    )
    if forced_text_tool is not None and finish_reason != "stop":
        incomplete = to_plain_data(
            getattr(state.completed_response, "incomplete_details", None)
        )
        raise ResponsesProtocolError(
            f"JSON Schema tool response incomplete: {incomplete!r}"
        )
    final_output = to_plain_data(getattr(state.completed_response, "output", None))
    final_items = _validated_response_items(final_output)
    if len(final_items) != len(state.output_items) or not all(
        _output_items_agree(streamed, terminal)
        for streamed, terminal in zip(state.output_items, final_items, strict=True)
    ):
        raise ResponsesProtocolError(
            "terminal Responses output disagrees with streamed output items"
        )
    state.output_items = [
        _merge_terminal_projection(streamed, terminal)
        for streamed, terminal in zip(state.output_items, final_items, strict=True)
    ]
    content = "".join(_output_text(item) for item in state.output_items) or None
    reasoning = "\n".join(text for text in (_reasoning_text(item) for item in state.output_items) if text) or None
    tool_calls: list[dict[str, Any]] = []
    for item in state.output_items:
        if item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item.get("name"), "arguments": item["arguments"]},
                }
            )
    if forced_text_tool is not None:
        if tool_calls:
            raise ResponsesProtocolError("JSON Schema tool response unexpectedly contained function calls")
        if not content:
            raise ResponsesEmptyOutputError("JSON Schema tool response contained no output text")
        response_id = getattr(state.completed_response, "id", None)
        if not isinstance(response_id, str) or not response_id:
            raise ResponsesProtocolError("JSON Schema tool response is missing response identity")
        try:
            tool_call, synthetic_item = project_forced_text_tool(
                forced_text_tool,
                content,
                response_identity=response_id,
            )
        except ValueError as exc:
            raise ResponsesProtocolError(str(exc)) from exc
        tool_calls = [tool_call]
        state.output_items = [item for item in state.output_items if item.get("type") == "reasoning"]
        state.output_items.append(synthetic_item)
        content = None
    content = rescue_empty_turn(content, tool_calls, reasoning)
    if not content and not tool_calls:
        raise ResponsesEmptyOutputError("response.completed contained no message or function call")
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=parse_responses_usage(
            state.completed_response,
            messages,
            content,
            tool_calls,
        ),
        finish_reason=(
            "tool_calls" if tool_calls and finish_reason == "stop" else finish_reason
        ),
        reasoning=reasoning,
        provider_items=state.output_items,
        provider_model=actual_model,
    )


def parse_responses_response(
    response: Any,
    messages: list[dict[str, Any]],
    *,
    expected_model: str,
    forced_text_tool: ForcedTextTool | None = None,
) -> LLMResponse:
    """Parse one completed non-streaming Responses object."""
    _validate_terminal_response(response, expected_model)
    state = _StreamState(completed_response=response)
    output = to_plain_data(getattr(response, "output", None))
    if not isinstance(output, list):
        raise ResponsesProtocolError("completed Responses object is missing output items")
    for item in output:
        event = type("OutputItemEvent", (), {"item": item, "output_index": len(state.output_items)})()
        _accept_output_item(event, state)
    return _parse_stream(state, messages, expected_model, forced_text_tool)


async def complete_responses(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    max_retries: int,
    *,
    tool_choice: Any = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    prompt_cache_namespace: str | None = None,
    response_session_id: str | None = None,
    first_event_timeout: float = 180.0,
    stream_idle_timeout: float = 180.0,
    round_timeout: float | None = None,
    provider_error_time_budget: RetryTimeBudget | None = None,
    stream: bool = True,
) -> LLMResponse:
    """Run one locally replayable Responses request and require typed completion."""
    converted_tools = _responses_tools(tools)
    forced_text_tool = _forced_text_tool(model, converted_tools, tool_choice)
    kwargs = _build_request_kwargs(
        model,
        messages,
        tools,
        temperature,
        tool_choice=tool_choice,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        prompt_cache_namespace=prompt_cache_namespace,
        response_session_id=response_session_id,
    )
    capabilities = model_capabilities(model)
    stream = stream and capabilities.supports_responses_streaming

    async def request_once() -> LLMResponse:
        if not stream:
            kwargs["stream"] = False
            response = await client.responses.create(**kwargs)
            return parse_responses_response(
                response,
                messages,
                expected_model=model,
                forced_text_tool=forced_text_tool,
            )

        state = await _create_and_consume_stream(
            client,
            kwargs,
            first_event_timeout,
            stream_idle_timeout,
            model,
        )
        return _parse_stream(state, messages, model, forced_text_tool)

    if provider_error_time_budget is not None:

        async def bounded_request_once() -> LLMResponse:
            if round_timeout is None:
                return await request_once()
            try:
                return await asyncio.wait_for(request_once(), timeout=round_timeout)
            except asyncio.TimeoutError as exc:
                raise TransientProviderError(f"Responses request timeout after {round_timeout:g}s") from exc

        return await with_retry(
            bounded_request_once,
            max_retries=max_retries,
            retry_time_budget=provider_error_time_budget,
        )

    async def run() -> LLMResponse:
        return await with_retry(request_once, max_retries=max_retries)

    try:
        if round_timeout is None:
            return await run()
        return await asyncio.wait_for(run(), timeout=round_timeout)
    except asyncio.TimeoutError as exc:
        raise ResponsesProtocolError(f"Responses round deadline exceeded after {round_timeout:g}s") from exc
