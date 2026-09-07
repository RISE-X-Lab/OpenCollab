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
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}

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

# Gateway saturation refusals arrive with NO HTTP status at all.  A gateway
# that meters concurrency in front of the provider rejects the request before
# any upstream response exists, so the SDK raises a bare ``APIError`` whose
# ``status_code`` is unset and whose text is the gateway's own wording.  Both
# the status set above and ``RETRYABLE_MESSAGE_FRAGMENTS`` miss it, so such a
# call was retried zero times.  Measured on luna-cmdprimary40 (2026-09-05,
# gpt-5.6-luna): 91 seat sessions ended on a provider error, of which 28 read
# "APIError: Concurrency limit exceeded for user, please retry later" and 2
# read "APIError: Upstream HTTP/2 stream failed" -- neither carries a status.
# Repeating the identical request after a backoff can succeed, so both are
# transient.
#
# The classification stays narrow in two ways at once, because the fragments
# below are ordinary English that an application error could also contain:
# the error must be a provider-SDK exception class AND carry no HTTP status.
# An error that does carry a status was already decided by
# ``RETRYABLE_STATUS_CODES`` above, so this rule can never flip a 400, a 401 or
# a 404 to retryable, and a plain ``ValueError`` never matches the class gate.
_STATUSLESS_PROVIDER_ERROR_CLASS_NAMES = frozenset(
    {
        "apierror",
        "openaierror",
        "anthropicerror",
    }
)
_TRANSIENT_GATEWAY_MESSAGE_FRAGMENTS = (
    "concurrency limit",
    "too many concurrent",
    "please retry later",
    "try again later",
    "stream failed",
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


def _http_status_of(error: BaseException) -> int | None:
    """Best-effort HTTP status for ``error`` (direct attribute or ``.response``)."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None)
        if isinstance(resp_status, int) and not isinstance(resp_status, bool):
            return resp_status
    return None


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
        if (
            class_name in _STATUSLESS_PROVIDER_ERROR_CLASS_NAMES
            and _http_status_of(current) is None
            and any(
                fragment in message
                for fragment in _TRANSIENT_GATEWAY_MESSAGE_FRAGMENTS
            )
        ):
            return True
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
