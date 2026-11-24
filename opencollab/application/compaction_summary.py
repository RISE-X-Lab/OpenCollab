"""Read-time summarizer: the model-backed body for ``AutoCompactShaper``.

``AutoCompactShaper`` (``application/shaping.py``) is the heaviest reactive
history layer. It is default-off until a ``SummarizerPort`` —
``Callable[[list[dict]], str]`` — is injected. This module provides that
callable, backed by the Claude Code-derived nine-section compaction prompt
(``application/compaction_prompt.py``).

Session execution awaits ``asummarize`` on its own event loop. The synchronous
callable remains available to independent callers through a worker bridge.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from opencollab.application._session_run_shared import (
    _INCOMPLETE_RESPONSE_REASONS,
    GenerationTimeoutError,
    _TeamBudgetStop,
    _TokenBudgetStop,
)
from opencollab.application.compaction_prompt import (
    build_summary_request,
    format_compact_summary,
    transcript_recovery_note,
)
from opencollab.application.run_control import _RunCancelled, _RunControlPolicyError
from opencollab.application.shaping.pipeline import _group_spans
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP
from opencollab.domain.token_estimation import estimate_request_tokens

# Depend on the completion response shape instead of a concrete LLM client.
ACompletePort = Callable[[list[dict[str, Any]]], Awaitable[Any]]

DEFAULT_FALLBACK_CHARS = 4_000
SUMMARY_CACHE_VERSION = "read-time-summary-v1"

def _fallback_content_text(content: Any) -> str:
    """Render common message content shapes without exposing binary payloads."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            parts.append("[unknown block]")
            continue

        block_type = block.get("type")
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("input_text")
        if isinstance(text, str) and text:
            parts.append(text)
            continue

        label = block_type if isinstance(block_type, str) and block_type else "unknown"
        parts.append(f"[{label} block]")
    return "\n".join(parts)

def run_coro_blocking(make_coro: Callable[[], Awaitable[Any]]) -> Any:
    """Run an awaitable to completion from a sync caller, loop-safe.

    Always executes on a dedicated worker thread with a fresh event loop, so it
    works whether or not the caller's thread already has a running loop (it does,
    inside the run loop). ``make_coro`` is a factory so the coroutine is created
    in the worker thread.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make_coro())).result()

class ReadTimeSummarizer:
    """Sync ``SummarizerPort`` that summarizes a segment via the async LLM.

    Returns the parsed ``<summary>`` prose (the ``<analysis>`` scratchpad is
    stripped), optionally followed by a transcript-recovery pointer.
    Single-request failures fall back to a bounded raw excerpt. Large segments
    are summarized in chronological, tool-aligned chunks within the model
    window, carrying the preceding summary into each subsequent request.
    """

    def __init__(
        self,
        acomplete: ACompletePort,
        *,
        transcript_path: str | None = None,
        custom_instructions: str | None = None,
        fallback_chars: int = DEFAULT_FALLBACK_CHARS,
        context_window: int | None = None,
        max_output_tokens: int = DEFAULT_MAX_TOKENS_PER_STEP,
        estimate_tokens: Callable[[list[dict[str, Any]]], int] = estimate_request_tokens,
        output_limit: Callable[[list[dict[str, Any]]], int] | None = None,
    ):
        self._acomplete = acomplete
        self._transcript_path = transcript_path
        self._custom_instructions = custom_instructions
        self._fallback_chars = max(0, fallback_chars)
        self._context_window = context_window
        self._max_output_tokens = max_output_tokens
        self._estimate_tokens = estimate_tokens
        self._output_limit = output_limit
        self._last_call_cacheable = False

    @property
    def cache_key(self) -> tuple[Any, ...]:
        """Namespace successful summaries by model closure and prompt settings."""
        return (
            SUMMARY_CACHE_VERSION,
            id(self._acomplete),
            self._transcript_path,
            self._custom_instructions,
            self._fallback_chars,
            self._context_window,
            self._max_output_tokens,
            id(self._estimate_tokens),
            id(self._output_limit),
        )

    @property
    def last_call_cacheable(self) -> bool:
        """Whether the most recent result was a real summary, not a fallback."""
        return self._last_call_cacheable

    def __call__(self, segment: list[dict[str, Any]]) -> str:
        return run_coro_blocking(lambda: self.asummarize(segment))

    async def asummarize(self, segment: list[dict[str, Any]]) -> str:
        self._last_call_cacheable = False
        request = build_summary_request(segment, custom_instructions=self._custom_instructions)
        chunked = not self._fits(request)
        try:
            summary = await self._summarize_chunks(segment) if chunked else await self._complete(request)
            if not summary:
                raise ValueError("The model returned an empty conversation summary")
        except (GenerationTimeoutError, _TeamBudgetStop, _TokenBudgetStop, _RunCancelled, _RunControlPolicyError):
            raise
        except Exception as exc:
            if chunked:
                message = f"Conversation summary could not include all earlier messages: {exc}"
                if self._transcript_path:
                    message += "\n" + transcript_recovery_note(self._transcript_path)
                raise ValueError(message) from exc
            return self._fallback(segment)

        if self._transcript_path:
            summary += "\n\n" + transcript_recovery_note(self._transcript_path)
        self._last_call_cacheable = True
        return summary

    async def _complete(self, request: list[dict[str, Any]]) -> str:
        response = await self._acomplete(request)
        finish_reason = getattr(response, "finish_reason", None)
        if finish_reason in _INCOMPLETE_RESPONSE_REASONS:
            interruption = "filtered" if finish_reason == "content_filter" else "truncated"
            raise ValueError(f"The model returned a {interruption} conversation summary")
        return format_compact_summary(getattr(response, "content", None) or "")

    def _fits(self, request: list[dict[str, Any]], *, allow_budget_stop: bool = False) -> bool:
        if not self._context_window or self._context_window <= 0:
            return True
        input_tokens = self._estimate_tokens(request)
        if input_tokens >= self._context_window:
            return False
        try:
            output_tokens = self._output_limit(request) if self._output_limit else self._max_output_tokens
        except _TokenBudgetStop:
            # Let a single group reach the actual request-budget preparation,
            # which can extend its soft allowance or propagate the budget stop.
            return allow_budget_stop
        return input_tokens + output_tokens <= self._context_window

    async def _summarize_chunks(self, segment: list[dict[str, Any]]) -> str:
        spans = _group_spans(segment)
        cursor = 0
        summary = ""
        while cursor < len(spans):
            prefix = ([{
                "role": "user",
                "content": (
                    "Summary of the preceding conversation. Update it with the messages "
                    "that follow, preserving user requests and applying later revisions "
                    "in chronological order.\n\n" + summary
                ),
            }] if summary else [])
            end = cursor
            request: list[dict[str, Any]] = []
            while end < len(spans):
                candidate = build_summary_request(
                    [*prefix, *segment[spans[cursor][0]:spans[end][1]]],
                    custom_instructions=self._custom_instructions,
                )
                if not self._fits(candidate, allow_budget_stop=end == cursor):
                    break
                request = candidate
                end += 1
            if end == cursor:
                raise ValueError(
                    f"Message group {cursor + 1} and the preceding summary exceed the "
                    f"{self._context_window}-token context window with summary output reserved"
                )
            summary = await self._complete(request)
            if not summary:
                raise ValueError("The model returned an empty conversation summary")
            cursor = end
        return summary

    def _fallback(self, segment: list[dict[str, Any]]) -> str:
        """Bounded raw excerpt when no model summary is available."""
        parts: list[str] = []
        for message in segment:
            content = _fallback_content_text(message.get("content"))
            if content:
                parts.append(f"[{message.get('role', '?')}]: {content}")
        text = "\n".join(parts)[: self._fallback_chars]
        return text or "[summary unavailable]"

__all__ = ["ACompletePort", "ReadTimeSummarizer", "run_coro_blocking"]
