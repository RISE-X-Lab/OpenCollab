"""The public LLM client facade — dispatches to per-provider modules."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

import openai

from opencollab.adapters.llm.anthropic_provider import complete_anthropic
from opencollab.adapters.llm.openai_provider import complete_openai
from opencollab.adapters.llm.providers import (
    RESPONSES,
    is_anthropic,
    normalize_wire_protocol,
    warn_provider_near_miss,
)
from opencollab.adapters.llm.responses_provider import complete_responses
from opencollab.adapters.llm.types import LLMResponse, model_context_window
from opencollab.adapters.llm.usage_ledger import record_api_usage

_ledger_lock = threading.Lock()


async def _record_api_usage_async(**kwargs: Any) -> None:
    def _locked_record() -> None:
        with _ledger_lock:
            record_api_usage(**kwargs)

    try:
        await asyncio.to_thread(_locked_record)
    except Exception:
        return


class LLMClient:
    """Provider-agnostic LLM client. Uses OpenAI SDK which works with any
    compatible endpoint (OpenAI, DeepSeek, Together, Ollama, vLLM, etc.).

    For Anthropic: set provider="anthropic" to use the dedicated Anthropic
    SDK path (history is converted from OpenAI format per request).
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str = "openai",
        wire_protocol: str = "chat_completions",
        max_retries: int = 3,
        request_timeout: float = 600.0,
        connect_timeout: float = 30.0,
        first_event_timeout: float = 180.0,
        stream_idle_timeout: float = 180.0,
    ):
        self.model = model
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self.request_timeout = request_timeout
        self.wire_protocol = normalize_wire_protocol(wire_protocol)
        self.first_event_timeout = first_event_timeout
        self.stream_idle_timeout = stream_idle_timeout

        warn_provider_near_miss(provider)
        if is_anthropic(provider):
            if self.wire_protocol == RESPONSES:
                raise ValueError("Responses wire protocol requires an OpenAI-compatible provider")
            import anthropic

            self.base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
            anthropic_kwargs: dict[str, Any] = {
                "api_key": api_key or os.environ.get("ANTHROPIC_API_KEY"),
                "timeout": request_timeout,
            }
            if self.base_url:
                anthropic_kwargs["base_url"] = self.base_url
            self._anthropic = anthropic.AsyncAnthropic(**anthropic_kwargs)
            self._openai = None
        else:
            self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
            self._openai = openai.AsyncOpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                base_url=self.base_url,
                timeout=openai.Timeout(request_timeout, connect=connect_timeout),
            )
            self._anthropic = None

    def context_window(self) -> int | None:
        """The model's context window in tokens, or ``None`` if unknown."""
        return model_context_window(self.model)

    async def close(self) -> None:
        """Close the provider SDK client owned by this facade."""
        client = self._anthropic or self._openai
        if client is not None:
            await client.close()

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
    ) -> LLMResponse:
        """Single-shot completion. Returns full response.

        ``thinking`` is OFF by default, so existing callers are unaffected. When
        on, ``thinking_params`` is the provider-specific reasoning payload (sent
        as ``extra_body`` on the OpenAI-compatible path).

        ``tool_choice`` is ``None`` by default (provider default, i.e. "auto");
        a caller may pass ``"required"`` to force the model to emit a tool call.

        ``top_p`` is ``None`` by default (provider default); when set it is sent
        as the nucleus-sampling knob. Unset == byte-identical request as today.
        """
        start = time.monotonic()
        try:
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
                    max_output_tokens=max_output_tokens,
                )
            elif self.wire_protocol == RESPONSES:
                response = await complete_responses(
                    self._openai,
                    self.model,
                    messages,
                    tools,
                    temperature,
                    self.max_retries,
                    tool_choice=tool_choice,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                    reasoning_effort=reasoning_effort,
                    first_event_timeout=self.first_event_timeout,
                    stream_idle_timeout=self.stream_idle_timeout,
                    round_timeout=self.request_timeout,
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
                    max_output_tokens=max_output_tokens,
                )
            await _record_api_usage_async(
                provider=self.provider,
                model=self.model,
                wire_protocol=self.wire_protocol,
                reasoning_effort=reasoning_effort,
                base_url=self.base_url,
                latency_s=time.monotonic() - start,
                status="success",
                response=response,
            )
            return response
        except Exception as exc:
            await _record_api_usage_async(
                provider=self.provider,
                model=self.model,
                wire_protocol=self.wire_protocol,
                reasoning_effort=reasoning_effort,
                base_url=self.base_url,
                latency_s=time.monotonic() - start,
                status="error",
                error=exc,
            )
            raise
