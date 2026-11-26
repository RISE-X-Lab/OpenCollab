"""Private Chat stream assembly and transport consumption."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypeVar

from opencollab.adapters.llm._chat_response import (
    _build_chat_response,
    _clean_provider_model,
    _normalize_tool_arguments,
    _reported_chat_usage,
)
from opencollab.adapters.llm._transport_wait import _TransportInterruption, _wait_for_transport
from opencollab.adapters.llm.errors import StreamedUsageUnavailableError, TransientProviderError
from opencollab.adapters.llm.first_token import CHAT_STREAM, begin_attempt, mark_first_token
from opencollab.adapters.llm.types import LLMResponse
from opencollab.application.async_timeout import await_owned_operation

# Streaming request fields stay framework-controlled so ``extra_body``
# cannot enable streaming. Omit both fields for non-streamed requests.
_STREAM_REQUEST_FIELDS: dict[str, Any] = {
    "stream": True,
    "stream_options": {"include_usage": True},
}

# The SDK retains undeclared delta keys in Pydantic extras. DeepSeek and
# DashScope use ``reasoning_content``, while OpenRouter uses ``reasoning``.
# Lock onto the first spelling carrying text to avoid duplicating reasoning
# when a gateway sends both.
_REASONING_DELTA_FIELDS: tuple[str, ...] = ("reasoning_content", "reasoning")

@dataclass
class _ToolCallSlot:
    """One accumulating tool call, keyed by the delta's ``index``."""

    call_id: str | None = None
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)

@dataclass
class _ChatStreamState:
    content_parts: list[str] = field(default_factory=list)
    refusal_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_field: str | None = None
    tool_slots: dict[int, _ToolCallSlot] = field(default_factory=dict)
    finish_reason: str | None = None
    usage_object: Any = None
    provider_model: str | None = None
    chunk_count: int = 0

@dataclass
class _UsageCarrier:
    """Minimal stand-in for the response object ``_parse_usage`` reads."""

    usage: Any = None

def _absorb_reasoning_delta(delta: Any, state: _ChatStreamState) -> None:
    fields = (
        (state.reasoning_field,)
        if state.reasoning_field is not None
        else _REASONING_DELTA_FIELDS
    )
    for name in fields:
        piece = getattr(delta, name, None)
        if isinstance(piece, str) and piece:
            state.reasoning_field = name
            state.reasoning_parts.append(piece)
            return

def _absorb_tool_call_delta(call_delta: Any, state: _ChatStreamState) -> None:
    """Merge one tool-call fragment into the slot its ``index`` names.

    ``index`` — not arrival order — identifies the call: with parallel tool
    calls the fragments of index 0 and index 1 interleave, so appending in
    arrival order splices one call's arguments onto another. That usually
    surfaces as invalid JSON, but two fragments can concatenate into *valid*
    JSON, which executes a tool with corrupted arguments and reports nothing.
    ``id`` and ``name`` arrive only on a call's first fragment (later fragments
    carry ``None`` or ``""``), so they are kept, never overwritten with blanks.
    """
    index = getattr(call_delta, "index", None)
    if not isinstance(index, int) or isinstance(index, bool):
        # An unlabelled fragment has no safe owner.
        raise TransientProviderError("streamed tool_call delta has no index")

    slot = state.tool_slots.get(index)
    if slot is None:
        slot = _ToolCallSlot()
        state.tool_slots[index] = slot

    call_id = getattr(call_delta, "id", None)
    if isinstance(call_id, str) and call_id:
        if slot.call_id is not None and slot.call_id != call_id:
            raise TransientProviderError(
                f"streamed tool_call index {index} changed id "
                f"{slot.call_id!r} -> {call_id!r}"
            )
        slot.call_id = call_id

    function = getattr(call_delta, "function", None)
    if function is None:
        return

    name = getattr(function, "name", None)
    if isinstance(name, str) and name:
        # Gateways may split tool names across fragments, so append each piece.
        slot.name_parts.append(name)

    arguments = getattr(function, "arguments", None)
    if isinstance(arguments, str) and arguments:
        slot.argument_parts.append(arguments)

def _absorb_chunk(chunk: Any, state: _ChatStreamState) -> None:
    state.chunk_count += 1

    model = getattr(chunk, "model", None)
    if state.provider_model is None:
        state.provider_model = _clean_provider_model(model)

    # The final usage chunk has an empty ``choices`` list. Read usage before
    # accessing choices.
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        state.usage_object = usage

    for choice in getattr(chunk, "choices", None) or ():
        # Filter by choice index so future ``n>1`` requests cannot interleave answers.
        if getattr(choice, "index", 0) != 0:
            continue

        # With ``include_usage``, finish_reason can precede the final chunk.
        finish = getattr(choice, "finish_reason", None)
        if isinstance(finish, str) and finish:
            state.finish_reason = finish

        delta = getattr(choice, "delta", None)
        if delta is None:
            continue

        text = getattr(delta, "content", None)
        if isinstance(text, str) and text:
            state.content_parts.append(text)

        refusal = getattr(delta, "refusal", None)
        if isinstance(refusal, str) and refusal:
            state.refusal_parts.append(refusal)

        _absorb_reasoning_delta(delta, state)

        for call_delta in getattr(delta, "tool_calls", None) or ():
            _absorb_tool_call_delta(call_delta, state)

def _finalize_tool_calls(
    state: _ChatStreamState, tools: list[dict] | None
) -> list[dict[str, Any]]:
    """Emit the assembled tool calls in first-seen index order.

    Validate the assembled call identity against the tools registered for this
    request. For a complete response, argument syntax is validated by the tool
    execution layer, which returns ``tool_error`` feedback so the model can
    correct malformed JSON on its next turn, as on the ordinary Chat path.
    """
    registered: set[str] = set()
    for tool in tools or ():
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            registered.add(function["name"])

    # Interrupted calls may lack fields. Session retains them only in the trace.
    validate = state.finish_reason not in {"length", "max_tokens", "content_filter"}
    finalized: list[dict[str, Any]] = []
    for index, slot in state.tool_slots.items():
        name = "".join(slot.name_parts)
        if validate and not name:
            raise TransientProviderError(
                f"streamed tool_call {index} never carried a name"
            )
        if validate and registered and name not in registered:
            raise TransientProviderError(
                f"streamed tool_call {index} assembled an unregistered name {name!r}"
            )
        if validate and not slot.call_id:
            raise TransientProviderError(
                f"streamed tool_call {index} never carried an id"
            )

        # Apply the shared Chat normalization, then preserve the argument text
        # for tool validation and model feedback.
        arguments = _normalize_tool_arguments("".join(slot.argument_parts))

        finalized.append({
            "id": slot.call_id or "",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return finalized

def _stream_state_to_response(
    state: _ChatStreamState,
    request_messages: list[dict],
    tools: list[dict] | None,
    *,
    require_reported_usage: bool = True,
) -> LLMResponse:
    if state.chunk_count == 0:
        raise TransientProviderError("chat completion stream produced no chunks")

    # A missing finish_reason indicates a broken stream. The session truncation
    # guard needs it to stop incomplete tool calls from being stored or executed.
    if state.finish_reason is None:
        raise TransientProviderError(
            "chat completion stream ended without a finish_reason after "
            f"{state.chunk_count} chunks"
        )

    # Preserve ``None`` for absent content because trajectories distinguish it
    # from an empty string.
    content: str | None = "".join(state.content_parts) or None
    refusal: str | None = "".join(state.refusal_parts) or None
    reasoning: str | None = "".join(state.reasoning_parts) or None
    tool_calls = _finalize_tool_calls(state, tools)

    response = _build_chat_response(
        content,
        reasoning,
        tool_calls,
        state.finish_reason,
        state.provider_model,
        usage_source=_UsageCarrier(usage=state.usage_object),
        usage_message={
            "content": content,
            "refusal": refusal,
            "reasoning_content": reasoning,
            "tool_calls": tool_calls or None,
        },
        request_messages=request_messages,
        tools=tools,
        refusal=refusal,
    )

    # Missing requested usage would replace provider counters with estimates in
    # budget accounting and the USD ledger.
    if require_reported_usage and response.usage.estimated:
        raise StreamedUsageUnavailableError(
            "chat completion stream reported no usable usage block "
            "(endpoint may not honor stream_options.include_usage)"
        )
    return response

_T = TypeVar("_T")

async def _wait_for_chat_transport(
    operation: Awaitable[_T],
    timeout: float,
    settle_cancelled: Callable[[_T], Awaitable[None]],
) -> _T:
    cancellation: asyncio.CancelledError | None = None

    def observe(interruption: _TransportInterruption) -> None:
        nonlocal cancellation
        if isinstance(interruption, asyncio.CancelledError):
            cancellation = interruption

    try:
        result = await _wait_for_transport(operation, timeout, on_interrupt=observe)
    except (Exception, asyncio.CancelledError) as exc:
        if cancellation is not None and exc is not cancellation:
            raise cancellation from exc
        raise
    if cancellation is not None:
        await settle_cancelled(result)
        raise cancellation
    return result

async def _next_chunk(
    iterator: Any, budget: float | None, *, stage: str, state: _ChatStreamState,
) -> Any:
    async def settle_usage(chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            state.usage_object = usage

    try:
        if budget is None:
            return await iterator.__anext__()
        return await _wait_for_chat_transport(iterator.__anext__(), budget, settle_usage)
    except asyncio.TimeoutError as exc:
        detail = "from the provider transport" if budget is None else f"after {budget:g}s"
        raise TransientProviderError(
            f"chat completion {stage} timeout {detail}"
        ) from exc

async def _close_stream(stream: Any, *, preserve_cancellation: bool = False) -> None:
    """Release the socket without replacing a finished read or its original error."""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        result = close()
        if asyncio.iscoroutine(result):
            await await_owned_operation(result, propagate_cancellation=not preserve_cancellation)
    except asyncio.CancelledError:
        if not preserve_cancellation:
            raise
    except Exception:
        logging.getLogger(__name__).debug("chat stream close failed", exc_info=True)

async def _consume_chat_stream(
    stream: Any, first_chunk_timeout: float | None, idle_timeout: float | None
) -> _ChatStreamState:
    """Drain one chat-completion stream into an aggregate state.

    Two distinct clocks, because the httpx ``timeout`` bounds a single socket
    read once the response is streamed: a stream that emits one byte every 599
    seconds would otherwise never trip anything. The per-call wall clock upstream
    is unchanged and still bounds the whole call.
    """
    state = _ChatStreamState()
    try:
        await _drain_chat_stream(stream, state, first_chunk_timeout, idle_timeout)
    except BaseException as exc:
        usage = _reported_chat_usage(state.usage_object)
        if usage is not None:
            exc.usage = usage
        raise
    return state

async def _drain_chat_stream(
    stream: Any, state: _ChatStreamState, first_chunk_timeout: float | None, idle_timeout: float | None
) -> None:
    iterator = stream.__aiter__()
    first = True
    cancelled = False
    try:
        while True:
            try:
                chunk = await _next_chunk(
                    iterator,
                    first_chunk_timeout if first else idle_timeout,
                    stage="first-chunk" if first else "stream-idle",
                    state=state,
                )
            except StopAsyncIteration:
                break
            if first:
                # Before ``_absorb_chunk``: the measurement is when the bytes
                # landed, not when parsing them finished.
                mark_first_token(CHAT_STREAM)
            first = False
            _absorb_chunk(chunk, state)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        await _close_stream(stream, preserve_cancellation=cancelled)

async def _create_and_consume_chat_stream(
    client: Any,
    kwargs: dict[str, Any],
    first_chunk_timeout: float | None,
    idle_timeout: float | None,
) -> _ChatStreamState:
    loop = asyncio.get_running_loop()
    deadline = None if first_chunk_timeout is None else loop.time() + first_chunk_timeout
    begin_attempt(streamed=True)
    try:
        if first_chunk_timeout is None:
            event_stream = await client.chat.completions.create(**kwargs)
        else:
            event_stream = await _wait_for_chat_transport(
                client.chat.completions.create(**kwargs),
                first_chunk_timeout,
                partial(_close_stream, preserve_cancellation=True),
            )
    except asyncio.TimeoutError as exc:
        detail = "from the provider transport" if first_chunk_timeout is None else f"after {first_chunk_timeout:g}s"
        raise TransientProviderError(f"chat completion first-chunk timeout {detail}") from exc

    # Opening the stream spends part of the first-chunk allowance; charge it.
    remaining = None if deadline is None else deadline - loop.time()
    if remaining is not None and remaining <= 0:
        await _close_stream(event_stream)
        raise TransientProviderError(
            f"chat completion first-chunk timeout after {first_chunk_timeout:g}s"
        )
    return await _consume_chat_stream(event_stream, remaining, idle_timeout)
