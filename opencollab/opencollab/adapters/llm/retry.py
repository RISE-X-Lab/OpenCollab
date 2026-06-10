"""Retry policy for transient LLM-provider errors."""

from __future__ import annotations

import asyncio
import random
from typing import Any

# HTTP statuses worth retrying: timeouts, conflicts, rate limits, server errors.
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

# Error-message fragments that signal a transient failure when no status is set.
RETRYABLE_MESSAGE_FRAGMENTS = ("rate limit", "429", "timeout", "temporarily unavailable", "overloaded")

# Small random jitter (seconds) added to each backoff to reduce thundering herd.
RETRY_JITTER_MAX_SECONDS = 0.25


async def with_retry(call_factory, max_retries: int) -> Any:
    """Retry transient provider errors with exponential backoff.

    Prioritizes Retry-After when available (OpenRouter/OpenAI-compatible).
    """
    attempt = 0
    while True:
        try:
            return await call_factory()
        except Exception as e:
            if attempt >= max_retries or not is_retryable_error(e):
                raise

            retry_after = extract_retry_after_seconds(e)
            base = retry_after if retry_after is not None else (2 ** attempt)
            delay = max(0.0, base + random.uniform(0.0, RETRY_JITTER_MAX_SECONDS))
            await asyncio.sleep(delay)
            attempt += 1


def is_retryable_error(error: Exception) -> bool:
    """Whether ``error`` looks transient (retryable status code or message)."""
    status = getattr(error, "status_code", None)
    if status in RETRYABLE_STATUS_CODES:
        return True

    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None)
        if resp_status in RETRYABLE_STATUS_CODES:
            return True

    msg = str(error).lower()
    return any(k in msg for k in RETRYABLE_MESSAGE_FRAGMENTS)


def extract_retry_after_seconds(error: Exception) -> float | None:
    """The Retry-After header value from ``error``'s response, if present."""
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
