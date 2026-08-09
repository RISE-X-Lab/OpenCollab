"""Retry policy for transient LLM-provider errors."""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from opencollab.adapters.llm.errors import is_context_overflow_error

# HTTP statuses worth retrying: timeouts, conflicts, rate limits, server errors.
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

# Error-message fragments that signal a transient failure when no status is set.
RETRYABLE_MESSAGE_FRAGMENTS = (
    "rate limit",
    "429",
    "timeout",
    "temporarily unavailable",
    "overloaded",
)

# The Anthropic/OpenAI SDKs wrap transport failures in provider-specific
# exception classes (for example ``APIConnectionError``) without attaching an
# HTTP status.  Compatible gateways commonly stringify those exceptions as
# simply ``Connection error``.  Keep the classification narrow: retry known
# transport exception class names and transport-layer message fragments, but
# do not retry an arbitrary application ``ValueError`` that happens to mention
# a connection.
_TRANSIENT_TRANSPORT_CLASS_NAMES = frozenset(
    {
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connecttimeout",
        "connectionabortederror",
        "connectionrefusederror",
        "connectionreseterror",
        "brokenpipeerror",
        "pooltimeout",
        "readerror",
        "readtimeout",
        "remoteprotocolerror",
        "sockettimeout",
        "timeouterror",
        "writeerror",
        "writetimeout",
    }
)
_TRANSIENT_TRANSPORT_MESSAGE_FRAGMENTS = (
    "connection error",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "server disconnected",
    "remote protocol error",
)

# Small random jitter (seconds) added to each backoff to reduce thundering herd.
RETRY_JITTER_MAX_SECONDS = 0.25
MAX_RETRY_AFTER_SECONDS = 300.0


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
    # A context overflow is never transient: the identical prompt will overflow
    # again. The session layer handles it (force-compact then retry once), so it
    # must not be futilely retried here — guard explicitly even though 400 is
    # absent from RETRYABLE_STATUS_CODES, in case an overflow's message ever
    # happens to contain a retryable fragment ("overloaded", "timeout", ...).
    if is_context_overflow_error(error):
        return False

    status = getattr(error, "status_code", None)
    if status in RETRYABLE_STATUS_CODES:
        return True

    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None)
        if resp_status in RETRYABLE_STATUS_CODES:
            return True

    msg = str(error).lower()
    if any(k in msg for k in RETRYABLE_MESSAGE_FRAGMENTS):
        return True

    # Follow the exception cause/context chain because SDK wrappers often
    # preserve the useful transport error only as ``__cause__``.
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        class_name = type(current).__name__.lower()
        if class_name in _TRANSIENT_TRANSPORT_CLASS_NAMES:
            return True
        message = str(current).lower()
        if any(
            fragment in message
            for fragment in _TRANSIENT_TRANSPORT_MESSAGE_FRAGMENTS
        ) and class_name in {
            "apiconnectionerror",
            "apitimeouterror",
            "connecterror",
            "connectionerror",
            "connectionreseterror",
            "remoteprotocolerror",
            "timeouterror",
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


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
