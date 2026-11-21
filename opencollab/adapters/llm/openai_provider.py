"""OpenAI-compatible request building and response parsing.

Works with any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together,
Ollama, vLLM, etc.) via the OpenAI SDK.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from opencollab.adapters.llm._attempt_usage import _combine_attempt_usage
from opencollab.adapters.llm._chat_response import _parse_response, _reported_chat_usage
from opencollab.adapters.llm._chat_response import _usage_int as _usage_int
from opencollab.adapters.llm._chat_stream import (
    _STREAM_REQUEST_FIELDS,
    _create_and_consume_chat_stream,
    _stream_state_to_response,
)
from opencollab.adapters.llm.first_token import (
    NOT_STREAMED,
    begin_attempt,
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
    model_capabilities,
    responses_sampling_supported,
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

def _validated_thinking_params(
    thinking_params: dict | None,
    *,
    controlled_fields: frozenset[str] = _FRAMEWORK_CONTROLLED_THINKING_FIELDS,
) -> dict:
    """Reject provider extensions that overwrite OpenCollab request fields."""
    if not isinstance(thinking_params, dict):
        raise ValueError("thinking_params must be an object")
    protected = sorted(controlled_fields & thinking_params.keys())
    if protected:
        raise ValueError(
            "thinking_params cannot override framework-controlled request field(s): "
            + ", ".join(protected)
        )
    return dict(thinking_params)

def _uses_reasoning_request_fields(model: str) -> bool:
    leaf = model.strip().lower().rsplit("/", 1)[-1]
    return _OPENAI_REASONING_MODEL_RE.match(leaf) is not None

def _thinking_request_params(
    model: str, thinking: bool, params: dict | None,
    *, controlled_fields: frozenset[str] = _FRAMEWORK_CONTROLLED_THINKING_FIELDS,
    infer_thinking_type: bool = True,
) -> dict:
    """Resolve a declared native protocol without mutating its configuration."""
    if thinking:
        return _validated_thinking_params(params, controlled_fields=controlled_fields) if params else {}
    configured = _validated_thinking_params(params, controlled_fields=controlled_fields) if params else {}
    disabled = {}
    if "enable_thinking" in configured:
        disabled["enable_thinking"] = False
    mode = configured.get("thinking")
    if isinstance(mode, dict) and mode.get("type") in {"enabled", "adaptive", "disabled"}:
        disabled["thinking"] = {"type": "disabled"}
    if not disabled:
        protocol = model_capabilities(model).chat_thinking_protocol
        if protocol == "enable_thinking":
            disabled["enable_thinking"] = False
        elif infer_thinking_type and protocol == "thinking.type":
            disabled["thinking"] = {"type": "disabled"}
    return disabled

def _openai_tool_choice(choice: NormalizedToolChoice | None) -> Any:
    if choice is None:
        return "auto"
    if choice.mode == "named":
        return {"type": "function", "function": {"name": choice.name}}
    return choice.mode

def _keeps_reasoning_content(
    model: str, thinking: bool, thinking_params: dict | None
) -> bool:
    """Resolve Chat history replay from the configured thinking protocol.

    Thinking tool continuations can require the recorded reasoning on every
    assistant message. The same history policy applies to both wire shapes.
    """
    if model_capabilities(model).requires_chat_reasoning_content:
        return True
    if isinstance(thinking_params, dict):
        mode = thinking_params.get("thinking")
        if isinstance(mode, dict) and mode.get("type") == "disabled":
            return False
        if thinking_params.get("enable_thinking") is False:
            return False
    return thinking

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
    keep_reasoning_content: bool | None = None,
) -> dict[str, Any]:
    params = _thinking_request_params(model, thinking, thinking_params)
    reasoning_model = _uses_reasoning_request_fields(model)
    sampling_supported = not reasoning_model
    leaf = model.strip().lower().rsplit("/", 1)[-1]
    if re.fullmatch(r"gpt-5\.(?:1|2|4)(?:-\d{4}-\d{2}-\d{2})?", leaf):
        sampling_supported = responses_sampling_supported(leaf, reasoning_effort)
    if keep_reasoning_content is None:
        keep_reasoning_content = _keeps_reasoning_content(model, thinking, params)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": _normalize_request_messages(
            messages, keep_reasoning_content=keep_reasoning_content
        ),
    }
    if sampling_supported:
        kwargs["temperature"] = temperature
    if top_p is not None and sampling_supported:
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
    if params:
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.update(params)
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

    ``keep_reasoning_content`` selects provider-native thinking continuation
    fields. Strict endpoints receive only their supported message fields.
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
    first_event_timeout: float | None = 180.0,
    stream_idle_timeout: float | None = 180.0,
    before_attempt: Callable[[], int | None] | None = None,
    on_attempt_usage: Callable[[Usage], None] | None = None,
) -> LLMResponse:
    """Single-shot completion against an OpenAI-compatible endpoint.

    ``stream`` is OFF by default. Both paths use the same request history and
    response fields. Streaming additionally captures first-token timing and
    provider reasoning deltas.
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
    )
    if stream:
        kwargs.update(_STREAM_REQUEST_FIELDS)
    failed_usages: list[Usage | None] = []

    def prepare_attempt() -> None:
        if before_attempt is not None and (limit := before_attempt()) is not None:
            field = "max_completion_tokens" if _uses_reasoning_request_fields(model) else "max_tokens"
            kwargs[field] = limit

    def report_failure(_exc: BaseException) -> None:
        if failed_usages and (usage := failed_usages[-1]) is not None and on_attempt_usage is not None:
            on_attempt_usage(usage)

    async def request_once() -> LLMResponse:
        # Retry stream creation and draining together. Creation returns at headers,
        # so its success alone cannot cover a later stream failure.
        state = None
        resp = None
        try:
            if not stream:
                begin_attempt(streamed=False, unavailable_reason=NOT_STREAMED)
                resp = await client.chat.completions.create(**kwargs)
                return _parse_response(resp, kwargs["messages"], kwargs.get("tools"))
            state = await _create_and_consume_chat_stream(
                client, kwargs, first_event_timeout, stream_idle_timeout
            )
            return _stream_state_to_response(
                state, kwargs["messages"], kwargs.get("tools")
            )
        except BaseException as exc:
            usage = (
                _reported_chat_usage(state.usage_object) if state is not None
                else _reported_chat_usage(resp.usage) if resp is not None
                else getattr(exc, "usage", None)
            )
            failed_usages.append(usage)
            raise

    try:
        response = await with_retry(
            request_once,
            max_retries=max_retries,
            retry_time_budget=provider_error_time_budget,
            before_attempt=prepare_attempt,
            on_failure=report_failure,
        )
    except BaseException as exc:
        usage = _combine_attempt_usage(failed_usages)
        if usage is not None:
            exc.usage = usage
        raise
    if failed_usages:
        response.usage = _combine_attempt_usage([*failed_usages, response.usage])
    return response
