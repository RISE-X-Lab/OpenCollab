"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from opencollab.adapters.llm.errors import (
    StreamedUsageUnavailableError,
    TransientProviderError,
)
from opencollab.adapters.llm.retry import RetryTimeBudget, with_retry
from opencollab.adapters.llm.tool_contracts import (
    NormalizedToolChoice,
    normalize_function_tools,
    normalize_tool_choice,
    validate_tool_choice_target,
)
from opencollab.adapters.llm.types import (
    LLMResponse,
    Usage,
    estimate_messages_tokens,
    model_capabilities,
    rescue_empty_turn,
    to_plain_data,
    usage_to_dict,
)

# ``extra_body`` is merged into the OpenAI SDK's request payload after the
# explicit keyword arguments. Provider-native thinking settings must therefore
# not replace fields whose values are set by OpenCollab for this request.
_FRAMEWORK_CONTROLLED_THINKING_FIELDS = frozenset({
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "reasoning_effort",
    "stream",
    "stream_options",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
})
_OPENAI_REASONING_MODEL_RE = re.compile(r"^(?:o(?:1|3|4)|gpt-5)(?:$|[-.])")


def _validated_thinking_params(thinking_params: dict | None) -> dict:
    """Reject provider extensions that overwrite OpenCollab request fields."""
    if not isinstance(thinking_params, dict):
        raise ValueError("thinking_params must be an object")
    protected = sorted(_FRAMEWORK_CONTROLLED_THINKING_FIELDS & thinking_params.keys())
    if protected:
        raise ValueError(
            "thinking_params cannot override framework-controlled request field(s): "
            + ", ".join(protected)
        )
    return dict(thinking_params)


def _uses_reasoning_request_fields(model: str) -> bool:
    leaf = model.strip().lower().rsplit("/", 1)[-1]
    return _OPENAI_REASONING_MODEL_RE.match(leaf) is not None


def _openai_tool_choice(choice: NormalizedToolChoice | None) -> Any:
    if choice is None:
        return "auto"
    if choice.mode == "named":
        return {"type": "function", "function": {"name": choice.name}}
    return choice.mode


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
    reasoning_effort: str | None = None,
    keep_reasoning_content: bool = True,
) -> dict[str, Any]:
    reasoning_model = _uses_reasoning_request_fields(model)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": _normalize_request_messages(
            messages, keep_reasoning_content=keep_reasoning_content
        ),
    }
    if not reasoning_model:
        kwargs["temperature"] = temperature
    # Nucleus sampling rides along ONLY when explicitly set; when None the key is
    # omitted so the request is byte-for-byte identical to today's behavior.
    if top_p is not None and not reasoning_model:
        kwargs["top_p"] = top_p
    if max_output_tokens is not None:
        token_field = "max_completion_tokens" if reasoning_model else "max_tokens"
        kwargs[token_field] = int(max_output_tokens)
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    converted_tools = normalize_function_tools(tools)
    choice = normalize_tool_choice(tool_choice)
    capabilities = model_capabilities(model)
    if converted_tools:
        kwargs["tools"] = converted_tools
        provider_choice = _openai_tool_choice(choice)
        if not capabilities.supports_forced_tool_choice and (
            provider_choice == "required" or isinstance(provider_choice, dict)
        ):
            provider_choice = "auto"
        else:
            validate_tool_choice_target(choice, converted_tools)
        kwargs["tool_choice"] = provider_choice
    elif capabilities.supports_forced_tool_choice:
        validate_tool_choice_target(choice, converted_tools)
    # Thinking passthrough: when on, the provider-specific reasoning params ride
    # along as ``extra_body`` (a valid OpenAI SDK create() kwarg) — for DashScope
    # compatible mode this is ``{"enable_thinking": True}``. When off, nothing is
    # added so the request is byte-for-byte unchanged. Merge into any existing
    # extra_body rather than clobbering it.
    if thinking and thinking_params:
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.update(_validated_thinking_params(thinking_params))
        kwargs["extra_body"] = extra_body
    return kwargs


# Message keys an OpenAI-compatible endpoint accepts on the request path.
_REQUEST_MESSAGE_FIELDS = frozenset({
    "role",
    "content",
    "reasoning_content",
    "tool_calls",
    "tool_call_id",
    "name",
})


def _normalize_request_messages(
    messages: list[dict], *, keep_reasoning_content: bool = True
) -> list[dict]:
    """Make message payloads acceptable to stricter OpenAI-compatible gateways.

    ``keep_reasoning_content=False`` drops recorded chain-of-thought from the
    outbound history. Streaming turns this on: streaming is what makes
    ``reasoning_content`` non-empty in the first place, and echoing it back
    would both inflate input tokens and diverge the request from the
    non-streaming baseline by more than the two streaming keys. The reasoning
    still reaches the trajectory — it is recorded, just not resent.
    """
    dropped = frozenset() if keep_reasoning_content else frozenset({"reasoning_content"})
    allowed = _REQUEST_MESSAGE_FIELDS - dropped
    normalized: list[dict] = []
    for message in messages:
        item = {
            key: value
            for key, value in message.items()
            if key in allowed
        }
        if item.get("content") is None:
            item["content"] = ""
        if item.get("role") == "assistant" and item.get("tool_calls") and item.get("content") == "":
            item["content"] = " "
        normalized.append(item)
    return normalized


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

    if (
        content.count(_MARKUP_SECTION_BEGIN) != 1
        or content.count(_MARKUP_SECTION_END) != 1
    ):
        return [], content
    start = content.index(_MARKUP_SECTION_BEGIN)
    section_start = start + len(_MARKUP_SECTION_BEGIN)
    end_idx = content.index(_MARKUP_SECTION_END, section_start)
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

    # Strip the validated markup section while preserving surrounding prose.
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
) -> LLMResponse:
    """Turn already-extracted response fields into an ``LLMResponse``.

    Both wire shapes (one non-streaming ``ChatCompletion`` and a reassembled
    chunk stream) end here, so the three contracts that live in this tail —
    kimi markup recovery, usage parsing with its estimate fallback, and the
    empty-turn rescue — apply to both by construction rather than by being
    hand-mirrored on each path.

    ``usage_source`` only needs a ``.usage`` attribute; ``usage_message`` is the
    assistant message (SDK object or plain dict) used to estimate output tokens
    when the endpoint reports none.
    """
    # kimi (DashScope compat) sometimes emits tool calls as literal special-token
    # markup instead of structured ``tool_calls`` — in ``content`` or, under
    # thinking mode, inside ``reasoning_content`` (finish_reason='stop', empty
    # ``message.tool_calls``). Recover them so the tool actually runs instead of
    # being treated as a prose stop.
    markup_recovered = False
    if not tool_calls:
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

    usage = _parse_usage(usage_source, request_messages, usage_message, tools)
    # Surface the P6 recovery as an observability counter (summed up the chain
    # into the run metrics) without altering the recovered response itself.
    usage.markup_recovered = 1 if markup_recovered else 0
    # Thinking providers (e.g. kimi-k2.6 with ``enable_thinking``) put the
    # chain-of-thought in ``reasoning_content`` and the answer in ``content``.
    # Keep the reasoning for trajectory observability; the shared rescue rung
    # falls back to it only when the turn is otherwise empty.
    content = rescue_empty_turn(content, tool_calls, reasoning)
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=reasoning,
        provider_model=provider_model,
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

    return _build_chat_response(
        message.content,
        getattr(message, "reasoning_content", None) or None,
        tool_calls,
        choice.finish_reason,
        _clean_provider_model(getattr(resp, "model", None)),
        usage_source=resp,
        usage_message=message,
        request_messages=request_messages,
        tools=tools,
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
    """Estimate output tokens from all serialized assistant response fields."""
    plain_message = to_plain_data(message)
    if not isinstance(plain_message, dict):
        return 0
    return estimate_messages_tokens([{"role": "assistant", **plain_message}])


# ---------------------------------------------------------------------------
# Streaming (opt-in): reassembling one chat completion from its chunks
# ---------------------------------------------------------------------------

# Extra request keys the streaming path adds, and the ONLY difference between a
# streamed request and today's. When streaming is off these are never built, so
# the OpenAI SDK omits both from the JSON body entirely (it drops parameters
# left at their ``omit`` sentinel) and the request stays byte-for-byte the one
# the existing runs were produced with. Both keys are also in
# ``_FRAMEWORK_CONTROLLED_THINKING_FIELDS``, so ``extra_body`` cannot switch
# streaming on from the side.
_STREAM_REQUEST_FIELDS: dict[str, Any] = {
    "stream": True,
    "stream_options": {"include_usage": True},
}

# Candidate delta keys carrying chain-of-thought text. The SDK's ``ChoiceDelta``
# declares neither, but its BaseModel is ``extra="allow"``, so an unknown wire
# key lands in pydantic extras and ``getattr`` finds it.
#   reasoning_content — DeepSeek / DashScope compatible mode (verified against
#                       the configured endpoint) and the spelling the
#                       non-streaming path already reads
#   reasoning         — OpenRouter-style gateways
# The first spelling that carries text wins and is then locked for the turn: a
# gateway that sends both would otherwise duplicate the whole chain-of-thought,
# and duplicated reasoning is invisible to a human reader.
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
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_field: str | None = None
    tool_slots: dict[int, _ToolCallSlot] = field(default_factory=dict)
    tool_order: list[int] = field(default_factory=list)
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
        # Guessing an owner for an unlabelled fragment is exactly the move that
        # answers wrongly in silence, so fail loudly instead.
        raise TransientProviderError("streamed tool_call delta has no index")

    slot = state.tool_slots.get(index)
    if slot is None:
        slot = _ToolCallSlot()
        state.tool_slots[index] = slot
        state.tool_order.append(index)

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
        # Append rather than assign: the configured endpoint sends the whole
        # name in the first fragment, but a gateway that splits it would
        # otherwise leave only the last piece ('apply_patch' -> 'patch').
        slot.name_parts.append(name)

    arguments = getattr(function, "arguments", None)
    if isinstance(arguments, str) and arguments:
        slot.argument_parts.append(arguments)


def _absorb_chunk(chunk: Any, state: _ChatStreamState) -> None:
    state.chunk_count += 1

    model = getattr(chunk, "model", None)
    if state.provider_model is None:
        state.provider_model = _clean_provider_model(model)

    # Usage rides on the final chunk, whose ``choices`` is an EMPTY list — so it
    # must be read before touching choices, and choices must never be indexed
    # blindly the way the non-streaming path indexes ``resp.choices[0]``.
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        state.usage_object = usage

    for choice in getattr(chunk, "choices", None) or ():
        # ``n`` is never set today, so there is only ever one choice. Filtering
        # costs nothing and stops a future ``n>1`` from interleaving two
        # completions into one answer.
        if getattr(choice, "index", 0) != 0:
            continue

        # finish_reason is not necessarily on the last chunk: with
        # include_usage the configured endpoint puts it on the second to last.
        # Record whichever chunk carries it.
        finish = getattr(choice, "finish_reason", None)
        if isinstance(finish, str) and finish:
            state.finish_reason = finish

        delta = getattr(choice, "delta", None)
        if delta is None:
            continue

        text = getattr(delta, "content", None)
        if isinstance(text, str) and text:
            state.content_parts.append(text)

        _absorb_reasoning_delta(delta, state)

        for call_delta in getattr(delta, "tool_calls", None) or ():
            _absorb_tool_call_delta(call_delta, state)


def _finalize_tool_calls(
    state: _ChatStreamState, tools: list[dict] | None
) -> list[dict[str, Any]]:
    """Emit the assembled tool calls in first-seen index order.

    Two checks turn assembly bugs into errors instead of wrong answers:
    the reassembled name must be one this request actually registered (we hold
    that list, so the check is free), and the reassembled arguments must parse
    as JSON. The second check differs from the non-streaming path on purpose —
    there, malformed arguments can only come from the model, while here they can
    also come from our own reassembly, and the two are indistinguishable
    downstream. ``responses_provider`` sets the same precedent.
    """
    registered: set[str] = set()
    for tool in tools or ():
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            registered.add(function["name"])

    finalized: list[dict[str, Any]] = []
    for index in state.tool_order:
        slot = state.tool_slots[index]
        name = "".join(slot.name_parts)
        if not name:
            raise TransientProviderError(
                f"streamed tool_call {index} never carried a name"
            )
        if registered and name not in registered:
            raise TransientProviderError(
                f"streamed tool_call {index} assembled an unregistered name {name!r}"
            )
        if not slot.call_id:
            raise TransientProviderError(
                f"streamed tool_call {index} never carried an id"
            )

        # Repair the '{}{' prefix first, then validate: the other order would
        # condemn a response the shared repair can still rescue.
        arguments = _normalize_tool_arguments("".join(slot.argument_parts))
        try:
            json.loads(arguments)
        except (TypeError, ValueError) as exc:
            raise TransientProviderError(
                f"streamed tool_call {slot.call_id!r} assembled invalid JSON arguments"
            ) from exc

        finalized.append({
            "id": slot.call_id,
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

    # A finish_reason always exists on the non-streaming path, so its absence
    # here means the stream broke, never that "the turn simply ended". Treating
    # a truncated turn as a complete one is the worst failure available: the
    # session's provider-truncation guard reads this field, and a tool call cut
    # in half would be stored and then actually executed, with nothing in the
    # trajectory marking it as damaged.
    if state.finish_reason is None:
        raise TransientProviderError(
            "chat completion stream ended without a finish_reason after "
            f"{state.chunk_count} chunks"
        )

    # No content fragments yields None, not "": both behave identically in the
    # rescue and history rungs, but the trajectory stores this value verbatim
    # and '"content": ""' is not '"content": null' to an analysis script.
    content: str | None = "".join(state.content_parts) or None
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
            "reasoning_content": reasoning,
            "tool_calls": tool_calls or None,
        },
        request_messages=request_messages,
        tools=tools,
    )

    # The stream asked for usage explicitly. Not getting it means the budget
    # meter, the USD ledger and every cross-arm token comparison would run on
    # ``_parse_usage``'s estimate, flagged only by a boolean nobody reads.
    if require_reported_usage and response.usage.estimated:
        raise StreamedUsageUnavailableError(
            "chat completion stream reported no usable usage block "
            "(endpoint may not honor stream_options.include_usage)"
        )
    return response


async def _next_chunk(iterator: Any, budget: float, *, stage: str) -> Any:
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout=budget)
    except asyncio.TimeoutError as exc:
        raise TransientProviderError(
            f"chat completion {stage} timeout after {budget:g}s"
        ) from exc


async def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        await result


async def _consume_chat_stream(
    stream: Any, first_chunk_timeout: float, idle_timeout: float
) -> _ChatStreamState:
    """Drain one chat-completion stream into an aggregate state.

    Two distinct clocks, because the httpx ``timeout`` bounds a single socket
    read once the response is streamed: a stream that emits one byte every 599
    seconds would otherwise never trip anything. The per-call wall clock upstream
    is unchanged and still bounds the whole call.
    """
    state = _ChatStreamState()
    iterator = stream.__aiter__()
    first = True
    try:
        while True:
            try:
                chunk = await _next_chunk(
                    iterator,
                    first_chunk_timeout if first else idle_timeout,
                    stage="first-chunk" if first else "stream-idle",
                )
            except StopAsyncIteration:
                break
            first = False
            _absorb_chunk(chunk, state)
    finally:
        await _close_stream(stream)
    return state


async def _create_and_consume_chat_stream(
    client: Any,
    kwargs: dict[str, Any],
    first_chunk_timeout: float,
    idle_timeout: float,
) -> _ChatStreamState:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + first_chunk_timeout
    try:
        event_stream = await asyncio.wait_for(
            client.chat.completions.create(**kwargs),
            timeout=first_chunk_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise TransientProviderError(
            f"chat completion first-chunk timeout after {first_chunk_timeout:g}s"
        ) from exc

    # Opening the stream spends part of the first-chunk allowance; charge it.
    remaining = deadline - loop.time()
    if remaining <= 0:
        await _close_stream(event_stream)
        raise TransientProviderError(
            f"chat completion first-chunk timeout after {first_chunk_timeout:g}s"
        )
    return await _consume_chat_stream(event_stream, remaining, idle_timeout)


async def complete_openai(
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
    reasoning_effort: str | None = None,
    provider_error_time_budget: RetryTimeBudget | None = None,
    stream: bool = False,
    first_event_timeout: float = 180.0,
    stream_idle_timeout: float = 180.0,
) -> LLMResponse:
    """Single-shot completion against an OpenAI-compatible endpoint.

    ``stream`` is OFF by default and, when off, this function executes exactly
    the code it always has: no streaming keys are built, so the SDK sends the
    same JSON body as before and the same parser reads the reply. Turning it on
    is the only way to capture ``reasoning_content``, which several endpoints
    (DeepSeek among them) return solely over the streamed wire format.
    """
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
        reasoning_effort,
        # Streaming is what makes reasoning non-empty; recording it must not
        # turn into resending it on the next turn.
        keep_reasoning_content=not stream,
    )
    if not stream:
        resp = await with_retry(
            lambda: client.chat.completions.create(**kwargs),
            max_retries=max_retries,
            retry_time_budget=provider_error_time_budget,
        )
        return _parse_response(resp, kwargs["messages"], kwargs.get("tools"))

    stream_kwargs = {**kwargs, **_STREAM_REQUEST_FIELDS}

    async def request_once() -> LLMResponse:
        # create() and the drain belong to the SAME retry unit. create()
        # returns as soon as the response headers land, so wrapping only it
        # would leave a mid-stream break outside the retry — a silent
        # degradation, since a half-received answer looks like a whole one.
        state = await _create_and_consume_chat_stream(
            client, stream_kwargs, first_event_timeout, stream_idle_timeout
        )
        return _stream_state_to_response(
            state, stream_kwargs["messages"], stream_kwargs.get("tools")
        )

    return await with_retry(
        request_once,
        max_retries=max_retries,
        retry_time_budget=provider_error_time_budget,
    )
