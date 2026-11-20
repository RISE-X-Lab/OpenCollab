"""The public LLM client facade — dispatches to per-provider modules."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import threading
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Callable
from urllib.parse import urlsplit

import openai

from opencollab.adapters.llm.anthropic_provider import (
    _anthropic_checks_thinking_prefix,
    _minimum_output_tokens,
    complete_anthropic,
)
from opencollab.adapters.llm.anthropic_provider import (
    _estimate_request_tokens as _estimate_anthropic_request_tokens,
)
from opencollab.adapters.llm.first_token import recording
from opencollab.adapters.llm.openai_provider import _build_request_kwargs, complete_openai
from opencollab.adapters.llm.providers import (
    RESPONSES,
    is_anthropic,
    is_dashscope_endpoint,
    normalize_provider,
    normalize_wire_protocol,
    warn_provider_near_miss,
)
from opencollab.adapters.llm.request_admission import request_slot
from opencollab.adapters.llm.responses_messages import messages_to_input
from opencollab.adapters.llm.responses_provider import complete_responses
from opencollab.adapters.llm.retry import RetryTimeBudget, _scoped_native_retry_budget
from opencollab.adapters.llm.types import LLMResponse, model_context_window
from opencollab.adapters.llm.usage_ledger import record_api_usage
from opencollab.application._session_run_usage import _observe_native_usage, _own_native_usage
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.ports import (
    _NATIVE_COMPLETION_CAPABILITY,
    _NATIVE_REQUEST_ADMISSION,
    _LegacyCompletionMessage,
    _native_completion_controls,
)
from opencollab.domain.token_estimation import estimate_request_tokens

# Native data-residency endpoints share the OpenAI model parameter rules.
_NATIVE_OPENAI_HOSTS = {
    "api.openai.com", "us.api.openai.com", "eu.api.openai.com", "au.api.openai.com",
    "ca.api.openai.com", "jp.api.openai.com", "in.api.openai.com", "sg.api.openai.com",
    "kr.api.openai.com", "gb.api.openai.com", "ae.api.openai.com",
}

_ledger_lock = threading.Lock()

def _dashscope_api_key(api_key: str | None) -> str:
    if api_key and api_key.strip():
        return api_key
    for name in ("DASHSCOPE_API_KEY", "OPENCOLLAB_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value
    raise ValueError("no API key configured for DashScope endpoint")

async def _record_api_usage_async(**kwargs: Any) -> None:
    def _locked_record() -> None:
        with _ledger_lock:
            record_api_usage(**kwargs)

    try:
        await await_owned_operation(asyncio.to_thread(_locked_record), propagate_cancellation=True)
    except Exception:
        return


class LLMClient:
    """Client for OpenAI-compatible endpoints and native Anthropic requests.

    Set ``provider="anthropic"`` for the Anthropic SDK path, which converts
    OpenAI-format history per request.
    """

    supports_request_revalidation = _NATIVE_COMPLETION_CAPABILITY
    supports_request_admission = _NATIVE_REQUEST_ADMISSION
    supports_attempt_usage = _NATIVE_COMPLETION_CAPABILITY
    supports_usage_settlement = _NATIVE_COMPLETION_CAPABILITY

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str = "openai",
        wire_protocol: str = "chat_completions",
        max_retries: int = 3,
        request_timeout: float | None = 600.0,
        connect_timeout: float | None = 30.0,
        first_event_timeout: float | None = 180.0,
        stream_idle_timeout: float | None = 180.0,
        context_window: int | None = None,
        provider_error_time_budget: float = 0.0,
        provider_retry_budget: RetryTimeBudget | None = None,
        user_agent: str | None = None,
        stream_chat: bool = False,
        auth_token: str | None = None,
        request_pool: str | None = None,
        request_capacity: int | None = None,
    ):
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window <= 0
        ):
            raise ValueError("context_window must be a positive integer or None")
        if (
            isinstance(provider_error_time_budget, bool)
            or not math.isfinite(provider_error_time_budget)
            or provider_error_time_budget < 0
        ):
            raise ValueError(
                "provider_error_time_budget must be a finite non-negative number"
            )
        if (request_pool is None) != (request_capacity is None):
            raise ValueError("request_pool and request_capacity must be supplied together")
        if request_capacity is not None and (
            isinstance(request_capacity, bool) or not isinstance(request_capacity, int) or request_capacity <= 0
        ):
            raise ValueError("request_capacity must be a positive integer")
        if request_pool is not None and not os.path.isabs(request_pool):
            raise ValueError("request_pool must be an absolute local directory path")
        self.request_pool = request_pool
        self.request_capacity = request_capacity
        self.model = model
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self.request_timeout = request_timeout
        self.wire_protocol = normalize_wire_protocol(wire_protocol)
        self.supports_response_session_identity = self.wire_protocol == RESPONSES
        self._responses_prompt_cache_namespace = (
            uuid.uuid4().hex if self.wire_protocol == RESPONSES else None
        )
        self.first_event_timeout = first_event_timeout
        self.stream_idle_timeout = stream_idle_timeout
        self.stream_chat = bool(stream_chat)
        self.provider_error_time_budget = provider_error_time_budget
        self._retry_budget_explicit = provider_retry_budget is not None
        if provider_retry_budget is not None:
            if provider_retry_budget.total_seconds != float(provider_error_time_budget):
                raise ValueError("shared provider retry budget does not match configuration")
            self.provider_retry_budget = provider_retry_budget
        else:
            self.provider_retry_budget = (
                RetryTimeBudget(provider_error_time_budget)
                if provider_error_time_budget > 0
                else None
            )
        self._context_window = context_window
        self._closed = False

        warn_provider_near_miss(provider)
        if is_anthropic(provider):
            if self.wire_protocol == RESPONSES:
                raise ValueError("Responses wire protocol requires an OpenAI-compatible provider")
            import anthropic

            self.base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
            anthropic_kwargs: dict[str, Any] = {
                "api_key": api_key or (None if auth_token else os.environ.get("ANTHROPIC_API_KEY")),
                "timeout": anthropic.Timeout(request_timeout, connect=connect_timeout),
                "max_retries": 0,
            }
            if auth_token and not api_key:
                anthropic_kwargs["auth_token"] = auth_token
                # Older SDKs also load an API key when a token is explicit.
                anthropic_kwargs["default_headers"] = {"X-Api-Key": anthropic.Omit()}
            elif api_key:
                anthropic_kwargs["default_headers"] = {"Authorization": anthropic.Omit()}
            if self.base_url:
                anthropic_kwargs["base_url"] = self.base_url
            self._anthropic = anthropic.AsyncAnthropic(**anthropic_kwargs)
            self._openai = None
        else:
            self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
            openai_kwargs: dict[str, Any] = {
                "api_key": (
                    _dashscope_api_key(api_key) if is_dashscope_endpoint(self.base_url)
                    else api_key or os.environ.get("OPENAI_API_KEY")
                ),
                "base_url": self.base_url,
                "timeout": openai.Timeout(request_timeout, connect=connect_timeout),
                "max_retries": 0,
            }
            resolved_user_agent = (
                user_agent or os.environ.get("OPENCOLLAB_LLM_USER_AGENT", "")
            ).strip()
            if resolved_user_agent:
                if len(resolved_user_agent.encode()) > 256 or any(
                    not 32 <= ord(char) <= 126 for char in resolved_user_agent
                ):
                    raise ValueError("user_agent must be at most 256 bytes of printable ASCII")
                openai_kwargs["default_headers"] = {"User-Agent": resolved_user_agent}
            self._openai = openai.AsyncOpenAI(**openai_kwargs)
            self._anthropic = None

    @property
    def requires_thinking_prefix(self) -> bool:
        """Whether native thinking binds to the preceding request fields."""
        return is_anthropic(self.provider) and _anthropic_checks_thinking_prefix(self.model)

    def estimate_request_tokens(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        thinking: bool = False,
        thinking_params: dict | None = None,
    ) -> int:
        """Reserve input from the message payload this client will send."""
        if is_anthropic(self.provider):
            return _estimate_anthropic_request_tokens(messages, tools)
        if self.wire_protocol != RESPONSES:
            request = _build_request_kwargs(
                self.model, messages, tools, 0.0, thinking, thinking_params
            )
            return estimate_request_tokens(request["messages"], request.get("tools"))
        projected = []
        for message in messages:
            instructions, items = messages_to_input([message], allow_unmatched_tool_outputs=True)
            if instructions:
                projected.append({"role": "system", "content": instructions})
            if message.get("role") not in {"system", "tool"} and message.get("response_items") is not None:
                # Preserve the existing native replay framing, including empty
                # replay lists, while excluding the legacy display copy.
                projected.append({"response_items": items})
            else:
                for item in items:
                    projected.append(item if "role" in item else {"response_items": [item]})
        return estimate_request_tokens(projected, tools, prefer_response_items=True)

    def estimate_context_tokens(
        self, messages: list[dict], *, thinking: bool = False, thinking_params: dict | None = None,
    ) -> int:
        """Additive native message sizing for history pressure, excluding tools."""
        kwargs = {"thinking": thinking, "thinking_params": thinking_params}
        framing = self.estimate_request_tokens([], **kwargs)
        return sum(max(0, self.estimate_request_tokens([message], **kwargs) - framing)
                   for message in messages)

    async def close(self) -> None:
        """Close the owned provider transport exactly once."""
        if self._closed:
            return
        self._closed = True
        provider_client = self._anthropic if self._anthropic is not None else self._openai
        close = getattr(provider_client, "close", None)
        if not callable(close):
            close = getattr(provider_client, "aclose", None)
        if callable(close):
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.close()

    def context_window(self) -> int | None:
        """The model's context window in tokens, or ``None`` if unknown."""
        return self._context_window or model_context_window(self.model)

    def minimum_output_tokens(
        self,
        *,
        max_output_tokens: int,
        thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
    ) -> int:
        """Output needed to issue a valid request with the configured thinking."""
        if self._anthropic:
            return _minimum_output_tokens(
                self.model, max_output_tokens=max_output_tokens,
                thinking=thinking, thinking_params=thinking_params,
            )
        return 1

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        tool_choice: Any = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        response_session_id: str | None = None,
        on_admitted: Callable[[], None] | None = None,
        before_request: Callable[[], int] | None = None,
        on_usage: Callable[[Any], None] | None = None,
        on_attempt_usage: Callable[[Any], None] | None = None,
    ) -> LLMResponse:
        """Single-shot completion. Returns full response.

        ``thinking`` is OFF by default. When enabled, ``thinking_params`` uses
        the selected provider's request shape. OpenAI-compatible providers
        receive it through ``extra_body``; a disabled request uses the native
        off switch of its declared or known protocol. Anthropic receives
        validated native ``thinking`` and optional ``output_config`` fields.

        ``tool_choice`` is ``None`` by default (provider default, i.e. "auto");
        a caller may pass ``"required"`` to force the model to emit a tool call.

        ``top_p`` is ``None`` by default (provider default); when set it is sent
        as the nucleus-sampling knob. Unset == byte-identical request as today.

        ``before_request`` runs before each physical attempt. Failed attempts
        report their known usage through ``on_attempt_usage`` before retry
        backoff. ``on_usage`` receives the final aggregate before slot release.
        """
        scope = _native_completion_controls.get()
        if scope is not None and scope.callbacks and scope.matches(messages, on_admitted):
            controls = scope.callbacks
            if on_admitted is None:
                on_admitted = controls.get("on_admitted")
            if before_request is None:
                before_request = controls.get("before_request")
            if on_attempt_usage is None:
                on_attempt_usage = controls.get("on_attempt_usage")
            if on_usage is None:
                on_usage = controls.get("on_usage")
            controls.clear()
        if scope is not None and scope.admission_callback is None:
            messages = [
                dict(message) if isinstance(message, _LegacyCompletionMessage) else message for message in messages
            ]

        start = time.monotonic()
        response = None
        provider_error: BaseException | None = None
        settlement_error: BaseException | None = None
        settled_usage = None
        first_attempt = True
        initial_limit: int | None = None

        def request_limit() -> int | None:
            limit = max_output_tokens
            if before_request is not None:
                current_limit = before_request()
                limit = current_limit if limit is None else min(limit, current_limit)
            return limit

        def prepare_attempt() -> int | None:
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                return initial_limit
            return request_limit()

        retry_budget = (
            self.provider_retry_budget
            if self._retry_budget_explicit
            else _scoped_native_retry_budget(self.provider_retry_budget)
        )
        try:
            async with AsyncExitStack() as request_owner:
                if on_usage is not None or on_attempt_usage is not None:
                    request_owner.enter_context(_own_native_usage())
                first_token = request_owner.enter_context(recording())
                try:
                    await request_owner.enter_async_context(request_slot(self.request_pool, self.request_capacity))
                    initial_limit = request_limit()
                    if on_admitted is not None:
                        on_admitted()
                    if self._anthropic:
                        response = await complete_anthropic(
                            self._anthropic,
                            self.model,
                            messages,
                            tools,
                            temperature,
                            self.max_retries,
                            thinking=thinking,
                            thinking_params=thinking_params,
                            tool_choice=tool_choice,
                            top_p=top_p,
                            max_output_tokens=initial_limit,
                            reasoning_effort=reasoning_effort,
                            provider_error_time_budget=retry_budget,
                            before_attempt=prepare_attempt,
                            on_attempt_usage=on_attempt_usage,
                        )
                    elif self.wire_protocol == RESPONSES:
                        response = await complete_responses(
                            self._openai,
                            self.model,
                            messages,
                            tools,
                            temperature,
                            self.max_retries,
                            thinking=thinking,
                            thinking_params=thinking_params,
                            tool_choice=tool_choice,
                            top_p=top_p,
                            max_output_tokens=initial_limit,
                            reasoning_effort=reasoning_effort,
                            prompt_cache_namespace=self._responses_prompt_cache_namespace,
                            response_session_id=response_session_id or uuid.uuid4().hex,
                            native_openai=(
                                normalize_provider(self.provider) == "openai"
                                and urlsplit(str(self._openai.base_url)).hostname in _NATIVE_OPENAI_HOSTS
                            ),
                            first_event_timeout=self.first_event_timeout,
                            stream_idle_timeout=self.stream_idle_timeout,
                            round_timeout=self.request_timeout,
                            provider_error_time_budget=retry_budget,
                            before_attempt=prepare_attempt,
                            on_attempt_usage=on_attempt_usage,
                        )
                    else:
                        response = await complete_openai(
                            self._openai,
                            self.model,
                            messages,
                            tools,
                            temperature,
                            self.max_retries,
                            thinking=thinking,
                            thinking_params=thinking_params,
                            tool_choice=tool_choice,
                            top_p=top_p,
                            max_output_tokens=initial_limit,
                            reasoning_effort=reasoning_effort,
                            provider_error_time_budget=retry_budget,
                            before_attempt=prepare_attempt,
                            on_attempt_usage=on_attempt_usage,
                            stream=self.stream_chat,
                            first_event_timeout=self.first_event_timeout,
                            stream_idle_timeout=self.stream_idle_timeout,
                        )
                except BaseException as exc:
                    provider_error = exc
                    raise
                finally:
                    settled_usage = getattr(response if response is not None else provider_error, "usage", None)
                    transport_timing = first_token.snapshot()
                    latency_s = time.monotonic() - start
                    if response is not None:
                        try:
                            response.transport_timing = transport_timing
                        except (AttributeError, TypeError):
                            pass
                    if settled_usage is not None:
                        try:
                            if on_usage is not None:
                                on_usage(settled_usage)
                            _observe_native_usage(
                                settled_usage, provider_error, transport_timing if response is not None else None,
                            )
                        except BaseException as exc:
                            exc.usage = settled_usage
                            settlement_error = exc
        finally:
            try:
                await _record_api_usage_async(
                    provider=self.provider,
                    model=self.model,
                    wire_protocol=self.wire_protocol,
                    reasoning_effort=reasoning_effort,
                    base_url=self.base_url,
                    latency_s=latency_s,
                    status="success" if response is not None else "error",
                    response=response,
                    error=provider_error,
                    transport_timing=transport_timing,
                )
            except BaseException as exc:
                if settled_usage is not None:
                    exc.usage = settled_usage
                raise
            finally:
                if settlement_error is not None:
                    raise settlement_error
        return response
