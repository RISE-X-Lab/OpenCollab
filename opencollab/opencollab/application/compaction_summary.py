"""Read-time summarizer: the model-backed body for ``AutoCompactShaper``.

``AutoCompactShaper`` (``application/shaping.py``) is the heaviest reactive
history layer. It is default-off until a ``SummarizerPort`` —
``Callable[[list[dict]], str]`` — is injected. This module provides that
callable, backed by the 9-section compaction prompt
(``application/compaction_prompt.py``).

The one friction it has to absorb: ``ShaperPort.shape`` is **sync**, but
``LLMPort.complete`` is **async**, and shaping runs inside the already-running
run-loop event loop. Nesting ``asyncio.run`` there is illegal, so the
completion is driven on a dedicated worker thread with its own event loop. That
blocks the calling loop for the duration of the (infrequent) summary call —
acceptable, since a compaction is a synchronous checkpoint by nature.

To avoid sharing an async HTTP client across event loops, the injected
``acomplete`` is expected to build whatever client it needs *inside* the
coroutine (see ``bootstrap/container.py``).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from opencollab.application.compaction_prompt import (
    build_summary_request,
    format_compact_summary,
    transcript_recovery_note,
)

# An async completion: given the summary request messages, return an object
# exposing ``.content`` (the model's raw text). Kept abstract so the summarizer
# depends only on this shape, not on any concrete LLM client.
ACompletePort = Callable[[list[dict[str, Any]]], Awaitable[Any]]

DEFAULT_FALLBACK_CHARS = 4_000


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
    stripped), optionally followed by a transcript-recovery pointer. On any
    failure — LLM error, empty response, or no ``<summary>`` block — it falls
    back to a bounded raw excerpt so the marker is never empty.
    """

    def __init__(
        self,
        acomplete: ACompletePort,
        *,
        transcript_path: str | None = None,
        custom_instructions: str | None = None,
        fallback_chars: int = DEFAULT_FALLBACK_CHARS,
    ):
        self._acomplete = acomplete
        self._transcript_path = transcript_path
        self._custom_instructions = custom_instructions
        self._fallback_chars = max(0, fallback_chars)

    def __call__(self, segment: list[dict[str, Any]]) -> str:
        request = build_summary_request(segment, custom_instructions=self._custom_instructions)
        try:
            response = run_coro_blocking(lambda: self._acomplete(request))
        except Exception:
            return self._fallback(segment)

        raw = getattr(response, "content", None) or ""
        summary = format_compact_summary(raw)
        if not summary:
            return self._fallback(segment)
        if self._transcript_path:
            summary += "\n\n" + transcript_recovery_note(self._transcript_path)
        return summary

    def _fallback(self, segment: list[dict[str, Any]]) -> str:
        """Bounded raw excerpt when no model summary is available."""
        parts: list[str] = []
        for message in segment:
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(f"[{message.get('role', '?')}]: {content}")
        text = "\n".join(parts)[: self._fallback_chars]
        return text or "[summary unavailable]"


__all__ = ["ACompletePort", "ReadTimeSummarizer", "run_coro_blocking"]
