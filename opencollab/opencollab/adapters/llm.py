"""Minimal LLM interface — thin wrapper over OpenAI-compatible API.

Supports any OpenAI-compatible provider (OpenAI, DeepSeek, local models)
and Anthropic natively. No custom message format — uses standard dicts.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import openai

# ---------------------------------------------------------------------------
# Response containers
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Single LLM completion result."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage())
    finish_reason: str | None = None
    raw: Any = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class StreamDelta:
    """A single chunk from streaming response."""

    content: str | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_args_delta: str | None = None
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Provider-agnostic LLM client. Uses OpenAI SDK which works with any
    compatible endpoint (OpenAI, DeepSeek, Together, Ollama, vLLM, etc.).

    For Anthropic: set base_url="https://api.anthropic.com/v1" and use
    anthropic-compatible proxy, or use the dedicated Anthropic SDK path.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str = "openai",
        max_retries: int = 3,
    ):
        self.model = model
        self.provider = provider
        self.max_retries = max(0, max_retries)

        if provider == "anthropic":
            import anthropic

            self._anthropic = anthropic.AsyncAnthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            )
            self._openai = None
        else:
            self._openai = openai.AsyncOpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            )
            self._anthropic = None

    async def _with_retry(self, call_factory, provider: str) -> Any:
        """Retry transient provider errors with exponential backoff.

        Prioritizes Retry-After when available (OpenRouter/OpenAI-compatible).
        """
        attempt = 0
        while True:
            try:
                return await call_factory()
            except Exception as e:
                if attempt >= self.max_retries or not self._is_retryable_error(e):
                    raise

                retry_after = self._extract_retry_after_seconds(e)
                base = retry_after if retry_after is not None else (2 ** attempt)
                # Add small jitter to reduce thundering herd.
                delay = max(0.0, base + random.uniform(0.0, 0.25))
                await asyncio.sleep(delay)
                attempt += 1

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        status = getattr(error, "status_code", None)
        if status in {408, 409, 429, 500, 502, 503, 504}:
            return True

        response = getattr(error, "response", None)
        if response is not None:
            resp_status = getattr(response, "status_code", None)
            if resp_status in {408, 409, 429, 500, 502, 503, 504}:
                return True

        msg = str(error).lower()
        return any(k in msg for k in ("rate limit", "429", "timeout", "temporarily unavailable", "overloaded"))

    @staticmethod
    def _extract_retry_after_seconds(error: Exception) -> float | None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None) if response is not None else None
        if not headers:
            return None

        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ---- Non-streaming completion ----

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Single-shot completion. Returns full response."""
        start = time.monotonic()
        if self._anthropic:
            return await self._complete_anthropic(messages, tools, temperature)
        return await self._complete_openai(messages, tools, temperature)

    async def _complete_openai(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await self._with_retry(
            lambda: self._openai.chat.completions.create(**kwargs),
            provider="openai",
        )
        choice = resp.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                })

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            ),
            finish_reason=choice.finish_reason,
            raw=resp,
        )

    async def _complete_anthropic(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> LLMResponse:
        system_parts, anthropic_messages = _convert_to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 8192,
            "temperature": temperature,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        if tools:
            anthropic_tools = []
            for t in tools:
                func = t["function"]
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
            kwargs["tools"] = anthropic_tools

        resp = await self._with_retry(
            lambda: self._anthropic.messages.create(**kwargs),
            provider="anthropic",
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
            usage=Usage(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            ),
            finish_reason=resp.stop_reason,
            raw=resp,
        )

    # ---- Streaming completion (OpenAI path only for now) ----

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamDelta]:
        """Streaming completion. Yields deltas."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if self._anthropic:
            async for delta in self._stream_anthropic(messages, tools, temperature):
                yield delta
            return

        stream = await self._openai.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish = chunk.choices[0].finish_reason

            # Text content
            if delta.content:
                yield StreamDelta(content=delta.content)

            # Tool calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    yield StreamDelta(
                        tool_call_index=tc.index,
                        tool_call_id=tc.id,
                        tool_call_name=tc.function.name if tc.function and tc.function.name else None,
                        tool_call_args_delta=tc.function.arguments if tc.function else None,
                    )

            if finish:
                yield StreamDelta(finish_reason=finish)

    async def _stream_anthropic(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> AsyncIterator[StreamDelta]:
        system_parts, anthropic_messages = _convert_to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 8192,
            "temperature": temperature,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if tools:
            anthropic_tools = []
            for t in tools:
                func = t["function"]
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
            kwargs["tools"] = anthropic_tools

        async with self._anthropic.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield StreamDelta(content=event.delta.text)
                    elif event.delta.type == "input_json_delta":
                        yield StreamDelta(tool_call_args_delta=event.delta.partial_json)
                elif event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        yield StreamDelta(
                            tool_call_index=event.index,
                            tool_call_id=event.content_block.id,
                            tool_call_name=event.content_block.name,
                        )
                elif event.type == "message_delta":
                    if hasattr(event.delta, "stop_reason") and event.delta.stop_reason:
                        yield StreamDelta(finish_reason=event.delta.stop_reason)


def _convert_to_anthropic_messages(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Convert OpenAI-format message history to Anthropic format.

    Returns (system_parts, anthropic_messages).

    Key conversions:
    - role="system" → extracted to system_parts (Anthropic uses top-level system param)
    - role="assistant" with tool_calls → assistant with tool_use content blocks
    - role="tool" → role="user" with tool_result content blocks (merged if consecutive)
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []

    for m in messages:
        role = m.get("role", "")

        if role == "system":
            system_parts.append(m.get("content", ""))

        elif role == "user":
            anthropic_messages.append({"role": "user", "content": m.get("content", "")})

        elif role == "assistant":
            content_blocks: list[dict] = []
            if m.get("content"):
                content_blocks.append({"type": "text", "text": m["content"]})
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    func = tc["function"]
                    try:
                        tool_input = json.loads(func["arguments"]) if isinstance(func["arguments"], str) else func["arguments"]
                    except (json.JSONDecodeError, TypeError):
                        tool_input = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": func["name"],
                        "input": tool_input,
                    })
            if content_blocks:
                anthropic_messages.append({"role": "assistant", "content": content_blocks})

        elif role == "tool":
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            }
            # Merge consecutive tool results into a single user message
            if (anthropic_messages
                    and anthropic_messages[-1]["role"] == "user"
                    and isinstance(anthropic_messages[-1]["content"], list)):
                anthropic_messages[-1]["content"].append(tool_result_block)
            else:
                anthropic_messages.append({"role": "user", "content": [tool_result_block]})

    return system_parts, anthropic_messages


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English, ~2 for CJK."""
    return max(1, len(text) // 3)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across a message list."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += estimate_tokens(part["text"])
    return total
