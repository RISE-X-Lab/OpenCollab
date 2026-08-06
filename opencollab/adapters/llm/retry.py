"""Retry policy for transient LLM-provider errors."""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from opencollab.adapters.llm.errors import TransientProviderError, is_context_overflow_error

# HTTP statuses worth retrying: timeouts, conflicts, rate limits, server errors.
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

# Error-message fragments that signal a transient failure when no status is set.
RETRYABLE_MESSAGE_FRAGMENTS = (
    "rate limit",
    "429",
    "timeout",
    "temporarily unavailable",
    "overloaded",
    "connection error",
    "connection reset",
    "connection refused",
    "server disconnected",
)

# Small random jitter (seconds) added to each backoff to reduce thundering herd.
RETRY_JITTER_MAX_SECONDS = 0.25
MAX_RETRY_AFTER_SECONDS = 300.0
MAX_EXPONENTIAL_RETRY_DELAY_SECONDS = 60.0


@dataclass
class RetryTimeBudget:
    """Shared retry-only time allowance for one agent or workflow run."""

    total_seconds: float
    remaining_seconds: float = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.total_seconds, bool)
            or not math.isfinite(self.total_seconds)
            or self.total_seconds < 0
        ):
            raise ValueError("retry time budget must be a finite non-negative number")
        self.remaining_seconds = float(self.total_seconds)

    def consume(self, seconds: float) -> bool:
        """Consume retry time atomically between event-loop suspension points."""
        seconds = max(0.0, seconds)
        if seconds > self.remaining_seconds:
            self.remaining_seconds = 0.0
            return False
        self.remaining_seconds -= seconds
        return True


async def with_retry(
    call_factory,
    max_retries: int,
    *,
    retry_time_budget: RetryTimeBudget | None = None,
) -> Any:
    """Retry transient provider errors with exponential backoff.

    Prioritizes Retry-After when available (OpenRouter/OpenAI-compatible).
    """
    attempt = 0
    while True:
        started = time.monotonic()
        try:
            return await call_factory()
        except Exception as e:
            if not is_retryable_error(e):
                raise
            retry_time_available = retry_time_budget is None or retry_time_budget.consume(
                time.monotonic() - started
            )
            if attempt >= max_retries:
                raise
            if not retry_time_available:
                raise

            retry_after = extract_retry_after_seconds(e)
            base = retry_after if retry_after is not None else 2.0 ** attempt
            if retry_time_budget is not None:
                base = min(base, MAX_EXPONENTIAL_RETRY_DELAY_SECONDS)
            delay = max(0.0, base + random.uniform(0.0, RETRY_JITTER_MAX_SECONDS))
            if retry_time_budget is not None and not retry_time_budget.consume(delay):
                raise
            await asyncio.sleep(delay)
            attempt += 1


def is_retryable_error(error: Exception) -> bool:
    """Whether ``error`` looks transient (retryable status code or message)."""
    if isinstance(error, TransientProviderError):
        return True

    # A context overflow is never transient: the identical prompt will overflow
    # again. The session layer handles it (force-compact then retry once), so it
    # must not be futilely retried here — guard explicitly even though 400 is
    # absent from RETRYABLE_STATUS_CODES, in case an overflow's message ever
    # happens to contain a retryable fragment ("overloaded", "timeout", ...).
    if is_context_overflow_error(error):
        return False

    response = getattr(error, "response", None)
    for value in (
        getattr(error, "status_code", None),
        getattr(response, "status_code", None) if response is not None else None,
    ):
        try:
            status = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 100 <= status <= 599:
            return status in RETRYABLE_STATUS_CODES

    msg = str(error).lower()
    return any(k in msg for k in RETRYABLE_MESSAGE_FRAGMENTS)


def extract_retry_after_seconds(
    error: Exception,
    *,
    now: datetime | None = None,
) -> float | None:
    """The Retry-After header value from ``error``'s response, if present."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None

    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        seconds = (retry_at - reference).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)
