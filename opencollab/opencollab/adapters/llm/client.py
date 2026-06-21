"""The public LLM client facade — dispatches to per-provider modules."""

from __future__ import annotations

import os
from typing import Any

import openai

from opencollab.adapters.llm.anthropic_provider import complete_anthropic
from opencollab.adapters.llm.openai_provider import complete_openai
from opencollab.adapters.llm.providers import is_anthropic, warn_provider_near_miss
from opencollab.adapters.llm.types import LLMResponse, model_context_window


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
        max_retries: int = 3,
        request_timeout: float = 600.0,
    ):
        self.model = model
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self.request_timeout = request_timeout

        warn_provider_near_miss(provider)
        if is_anthropic(provider):
            import anthropic

            self._anthropic = anthropic.AsyncAnthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
                timeout=request_timeout,
            )
            self._openai = None
        else:
            self._openai = openai.AsyncOpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
                timeout=request_timeout,
            )
            self._anthropic = None

    def context_window(self) -> int | None:
        """The model's context window in tokens, or ``None`` if unknown."""
        return model_context_window(self.model)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Single-shot completion. Returns full response.

        ``thinking`` is OFF by default, so existing callers are unaffected. When
        on, ``thinking_params`` is the provider-specific reasoning payload (sent
        as ``extra_body`` on the OpenAI-compatible path).
        """
        if self._anthropic:
            return await complete_anthropic(
                self._anthropic,
                self.model,
                messages,
                tools,
                temperature,
                self.max_retries,
                thinking=thinking,
                thinking_params=thinking_params,
            )
        return await complete_openai(
            self._openai,
            self.model,
            messages,
            tools,
            temperature,
            self.max_retries,
            thinking=thinking,
            thinking_params=thinking_params,
        )
