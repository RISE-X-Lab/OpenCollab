"""OpenAI Responses API request conversion and typed stream parsing."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from opencollab.adapters.llm._responses_instructions import _check_instructions_echo
from opencollab.adapters.llm._responses_output import (
    _merge_terminal_projection,
    _output_items_agree,
    _output_text,
    _reasoning_text,
)
from opencollab.adapters.llm._responses_output import (
    _semantic_output_item as _semantic_output_item,
)
from opencollab.adapters.llm.errors import TransientProviderError
from opencollab.adapters.llm.first_token import NOT_STREAMED, RESPONSES_STREAM, begin_attempt, mark_first_token
from opencollab.adapters.llm.responses_errors import (
    _TRANSIENT_RESPONSE_CODES,
    _TRANSIENT_RESPONSE_MESSAGES,
    ResponsesEmptyOutputError,
    ResponsesProtocolError,
    ResponsesStreamInterruptedError,
    ResponsesTerminalEventError,
    ResponsesTransientEventError,
)
from opencollab.adapters.llm.responses_messages import (
    OUTPUT_ITEM_TYPES as _OUTPUT_ITEM_TYPES,
)
from opencollab.adapters.llm.responses_messages import (
    function_call_identity as _function_call_identity,
)
from opencollab.adapters.llm.responses_messages import (
    messages_to_input as _messages_to_input,
)
from opencollab.adapters.llm.responses_messages import (
    validated_response_items as _validated_response_items,
)
from opencollab.adapters.llm.responses_structured import (
    ForcedTextTool,
    forced_text_format,
    forced_text_tool,
    project_forced_text_tool,
)
from opencollab.adapters.llm.responses_usage import (
    _combine_responses_usage,
    _reported_responses_usage,
    parse_responses_usage,
)
from opencollab.adapters.llm.retry import RetryTimeBudget, with_retry
from opencollab.adapters.llm.tool_contracts import (
    normalize_function_tools,
    normalize_tool_choice,
    validate_tool_choice_target,
)
from opencollab.adapters.llm.types import (
    LLMResponse,
    ModelCapabilities,
    Usage,
    model_capabilities,
    rescue_empty_turn,
    responses_sampling_supported,
    to_plain_data,
)

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
    *,
    capabilities: ModelCapabilities | None = None,
) -> ForcedTextTool | None:
    """Bind one named tool through ``text.format`` when forcing is unsupported."""
    choice = _responses_tool_choice(tool_choice, converted_tools)
    capabilities = capabilities or model_capabilities(model)
    try:
        return forced_text_tool(
            converted_tools,
            choice,
            supports_forced_tool_choice=capabilities.supports_forced_tool_choice,
            supports_json_schema=capabilities.supports_responses_json_schema,
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
    native_openai: bool = True,
) -> dict[str, Any]:
    instructions, input_items = _messages_to_input(messages)
    if not input_items:
        raise ResponsesProtocolError("Responses request has no input items")
    capabilities = model_capabilities(model)
    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": False,
        "stream": capabilities.supports_responses_streaming,
    }
    if capabilities.supports_responses_reasoning:
        kwargs["include"] = ["reasoning.encrypted_content"]
    if responses_sampling_supported(model, reasoning_effort, native_openai=native_openai):
        kwargs["temperature"] = temperature
    elif top_p is not None:
        raise ResponsesProtocolError(f"model {model!r} does not support explicit top_p")
    if reasoning_effort is not None and not capabilities.supports_responses_reasoning:
        raise ResponsesProtocolError(f"model {model!r} does not support explicit reasoning_effort")
    if instructions:
        kwargs["instructions"] = instructions
    elif os.environ.get("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO") == "1":
        # Native summarization requests can intentionally have no system text.
        # Make that absence explicit instead of inviting a gateway default.
        kwargs["instructions"] = ""
    converted_tools = _responses_tools(tools)
    if converted_tools and not capabilities.supports_responses_tools:
        raise ResponsesProtocolError(f"model {model!r} does not support function tools")
    choice = _responses_tool_choice(tool_choice, converted_tools)
    if converted_tools:
        text_tool = _forced_text_tool(
            model,
            converted_tools,
            tool_choice,
            capabilities=capabilities,
        )
        if text_tool is not None:
            kwargs["text"] = forced_text_format(text_tool)
        else:
            kwargs["tools"] = converted_tools
            if not capabilities.supports_forced_tool_choice and (
                choice == "required" or isinstance(choice, dict)
            ):
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


async def _next_event(iterator: Any, timeout: float | None, *, stage: str) -> Any:
    try:
        if timeout is None:
            return await iterator.__anext__()
        return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        detail = "from the provider transport" if timeout is None else f"after {timeout:g}s"
        raise ResponsesStreamInterruptedError(
            f"Responses {stage} timeout {detail}"
        ) from exc
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
                "param": plain_event.get("param"),
            }
    if error is None:
        response = getattr(event, "response", None)
        error = to_plain_data(getattr(response, "error", None))
        if error is None:
            error = to_plain_data(getattr(response, "incomplete_details", None))
    return error


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error.get("reason") or "unknown Responses error")
    return str(error or "unknown Responses error")


def _raise_response_error(error: Any) -> None:
    message = _error_message(error)
    code = error.get("code") if isinstance(error, dict) else None
    param = error.get("param") if isinstance(error, dict) else None
    if code in _TRANSIENT_RESPONSE_CODES or any(
        fragment in message.lower() for fragment in _TRANSIENT_RESPONSE_MESSAGES
    ):
        status_code = 429 if code == "rate_limit_exceeded" else 503
        raise ResponsesTransientEventError(message, code=code, status_code=status_code, param=param)
    status_code = 400 if code in {"context_length_exceeded", "string_above_max_length"} else None
    raise ResponsesTerminalEventError(message, code=code, status_code=status_code, param=param)


def _response_model(response: Any) -> str:
    actual_model = getattr(response, "model", None)
    if not isinstance(actual_model, str) or not actual_model:
        raise ResponsesProtocolError("terminal Responses object is missing model identity")
    return actual_model


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
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesProtocolError(f"function_call {call_id!r} is missing name")
        if item.get("status") != "incomplete":
            try:
                _function_call_identity(call_id, name, arguments)
            except ResponsesProtocolError as exc:
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
            raise ResponsesProtocolError(f"incomplete Responses object has unsupported reason {reason!r}")
        finish_reason = "max_tokens"
    return _response_model(response), finish_reason



def _handle_event(event: Any, state: _StreamState, expected_model: str | None = None) -> bool:
    event_type = _event_type(event)
    if event_type in {"error", "response.failed"}:
        state.completed_response = getattr(event, "response", None)
        _raise_response_error(_event_error_data(event))
    if event_type == "response.incomplete":
        response = getattr(event, "response", None)
        if getattr(response, "status", None) != "incomplete":
            raise ResponsesTerminalEventError(_error_message(_event_error_data(event)))
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
    first_event_timeout: float | None,
    idle_timeout: float | None,
    expected_model: str | None = None,
) -> _StreamState:
    state = _StreamState()
    try:
        await _drain_stream(stream, state, first_event_timeout, idle_timeout, expected_model)
    except BaseException as exc:
        usage = _reported_responses_usage(state.completed_response)
        if usage is not None:
            exc.usage = usage
        raise
    return state


async def _drain_stream(
    stream: Any,
    state: _StreamState,
    first_event_timeout: float | None,
    idle_timeout: float | None,
    expected_model: str | None,
) -> None:
    # OpenAI AsyncStream already owns its response-closing iterator and also
    # exposes a wrapper-style __aiter__ async generator.  Iterating the stream
    # directly avoids leaving that outer generator for interpreter shutdown,
    # where it can athrow into an already closed httpcore PoolByteStream.
    iterator = stream if callable(getattr(stream, "__anext__", None)) else stream.__aiter__()
    first = True
    try:
        while True:
            event = await _next_event(
                iterator,
                first_event_timeout if first else idle_timeout,
                stage="first-event" if first else "stream-idle",
            )
            if first:
                mark_first_token(RESPONSES_STREAM)
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
        _, finish_reason = _validate_terminal_response(state.completed_response, expected_model)
        output = _validated_response_items(to_plain_data(getattr(state.completed_response, "output", None)))
        covered = finish_reason == "max_tokens" and all(
            0 <= index < len(output)
            and output[index].get("type") == "function_call"
            and output[index].get("status") == "incomplete"
            and output[index].get("arguments") == "".join(fragments)
            for index, fragments in state.argument_fragments.items()
        )
        if not covered:
            raise ResponsesProtocolError("Responses stream ended with incomplete tool arguments")
        state.argument_fragments.clear()


async def _create_and_consume_stream(
    client: Any,
    kwargs: dict[str, Any],
    first_event_timeout: float | None,
    idle_timeout: float | None,
    expected_model: str,
) -> _StreamState:
    loop = asyncio.get_running_loop()
    deadline = None if first_event_timeout is None else loop.time() + first_event_timeout
    begin_attempt(streamed=True)
    try:
        if first_event_timeout is None:
            event_stream = await client.responses.create(**kwargs)
        else:
            event_stream = await asyncio.wait_for(
                client.responses.create(**kwargs),
                timeout=first_event_timeout,
            )
    except asyncio.TimeoutError as exc:
        detail = "from the provider transport" if first_event_timeout is None else f"after {first_event_timeout:g}s"
        raise ResponsesStreamInterruptedError(
            f"Responses first-event timeout {detail}"
        ) from exc

    remaining = None if deadline is None else deadline - loop.time()
    if remaining is not None and remaining <= 0:
        close = getattr(event_stream, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        raise ResponsesStreamInterruptedError(
            f"Responses first-event timeout after {first_event_timeout:g}s"
        )
    return await _consume_stream(
        event_stream,
        remaining,
        idle_timeout,
        expected_model,
    )


def _parse_stream(
    state: _StreamState,
    messages: list[dict[str, Any]],
    expected_model: str | None = None,
    forced_text_tool: ForcedTextTool | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> LLMResponse:
    actual_model, finish_reason = _validate_terminal_response(
        state.completed_response,
        expected_model,
    )
    final_output = to_plain_data(getattr(state.completed_response, "output", None))
    final_items = _validated_response_items(final_output)
    interrupted_calls = any(
        item.get("type") == "function_call" and item.get("status") == "incomplete"
        for item in final_items
    )
    if interrupted_calls:
        if finish_reason != "max_tokens":
            raise ResponsesProtocolError("completed Responses output contains incomplete tool calls")
        final_items = [
            item for item in final_items
            if item.get("type") != "function_call" or item.get("status") != "incomplete"
        ]
        state.output_items = [
            item for item in state.output_items
            if item.get("type") != "function_call" or item.get("status") != "incomplete"
        ]
    output_mismatch = len(final_items) != len(state.output_items) or not all(
        _output_items_agree(streamed, terminal)
        for streamed, terminal in zip(state.output_items, final_items, strict=True)
    )
    if output_mismatch:
        compatibility_enabled = (
            os.environ.get(
                "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH"
            )
            == "1"
        )
        if not compatibility_enabled or not state.output_items:
            raise ResponsesProtocolError(
                "terminal Responses output disagrees with streamed output items"
            )
    else:
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
    if forced_text_tool is not None and tool_calls:
        raise ResponsesProtocolError("JSON Schema tool response unexpectedly contained function calls")
    if forced_text_tool is not None and finish_reason == "stop":
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
    if not content and not tool_calls and finish_reason != "max_tokens":
        raise ResponsesEmptyOutputError("response.completed contained no message or function call")
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=parse_responses_usage(
            state.completed_response,
            messages,
            content,
            tool_calls,
            tools,
        ),
        finish_reason=("tool_calls" if tool_calls and finish_reason == "stop" else finish_reason),
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
    tools: list[dict[str, Any]] | None = None,
) -> LLMResponse:
    """Parse one terminal non-streaming Responses object."""
    if getattr(response, "status", None) == "failed":
        _response_model(response)
        _raise_response_error(to_plain_data(getattr(response, "error", None)))
    _validate_terminal_response(response, expected_model)
    state = _StreamState(completed_response=response)
    output = to_plain_data(getattr(response, "output", None))
    if not isinstance(output, list):
        raise ResponsesProtocolError("completed Responses object is missing output items")
    for item in output:
        event = type("OutputItemEvent", (), {"item": item, "output_index": len(state.output_items)})()
        _accept_output_item(event, state)
    return _parse_stream(
        state,
        messages,
        expected_model,
        forced_text_tool,
        tools,
    )


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
    native_openai: bool = True,
    first_event_timeout: float | None = 180.0,
    stream_idle_timeout: float | None = 180.0,
    round_timeout: float | None = None,
    provider_error_time_budget: RetryTimeBudget | None = None,
    stream: bool = True,
) -> LLMResponse:
    """Run one locally replayable Responses request and require typed completion."""
    external_isolation = os.environ.get("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION") == "1"
    retry_limit = None if external_isolation else max_retries
    retry_budget = None if external_isolation else provider_error_time_budget
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
        native_openai=native_openai,
    )
    capabilities = model_capabilities(model)
    stream = stream and capabilities.supports_responses_streaming
    failed_usages: list[Usage | None] = []

    async def request_once() -> LLMResponse:
        terminal = None
        try:
            if not stream:
                kwargs["stream"] = False
                begin_attempt(streamed=False, unavailable_reason=NOT_STREAMED)
                terminal = await client.responses.create(**kwargs)
                _check_instructions_echo(terminal, kwargs)
                return parse_responses_response(
                    terminal,
                    messages,
                    expected_model=model,
                    forced_text_tool=forced_text_tool,
                    tools=converted_tools,
                )

            state = await _create_and_consume_stream(
                client, kwargs, first_event_timeout, stream_idle_timeout, model,
            )
            terminal = state.completed_response
            _check_instructions_echo(terminal, kwargs)
            return _parse_stream(
                state,
                messages,
                model,
                forced_text_tool,
                converted_tools,
            )
        except BaseException as exc:
            failed_usages.append(_reported_responses_usage(terminal) or getattr(exc, "usage", None))
            raise

    if retry_budget is not None:

        async def bounded_request_once() -> LLMResponse:
            if round_timeout is None:
                return await request_once()
            try:
                return await asyncio.wait_for(request_once(), timeout=round_timeout)
            except asyncio.TimeoutError as exc:
                raise TransientProviderError(f"Responses request timeout after {round_timeout:g}s") from exc

    async def run() -> LLMResponse:
        if retry_budget is not None:
            return await with_retry(
                bounded_request_once, max_retries=retry_limit, retry_time_budget=retry_budget,
            )
        return await with_retry(request_once, max_retries=retry_limit)

    try:
        try:
            if round_timeout is None or retry_budget is not None:
                response = await run()
            else:
                response = await asyncio.wait_for(run(), timeout=round_timeout)
        except asyncio.TimeoutError as exc:
            raise ResponsesProtocolError(f"Responses round deadline exceeded after {round_timeout:g}s") from exc
    except BaseException as exc:
        usage = _combine_responses_usage(failed_usages)
        if usage is not None:
            exc.usage = usage
        raise
    if failed_usages:
        response.usage = _combine_responses_usage([*failed_usages, response.usage])
    return response
