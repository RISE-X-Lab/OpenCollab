"""Retry policy for transient LLM-provider errors."""

from __future__ import annotations

import asyncio
import contextvars
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar

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

# Gateways may reject a request before an upstream HTTP response exists.
# Restrict their retry hints to statusless provider SDK errors.
_STATUSLESS_PROVIDER_ERROR_CLASS_NAMES = frozenset({"apierror", "openaierror", "anthropicerror"})
_TRANSIENT_GATEWAY_MESSAGE_FRAGMENTS = (
    "concurrency limit",
    "too many concurrent",
    "stream failed",
)

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

_P = ParamSpec("_P")
_T = TypeVar("_T")
_NATIVE_RUN_RETRY_BUDGETS: contextvars.ContextVar[dict[int, tuple[RetryTimeBudget, RetryTimeBudget]] | None] = (
    contextvars.ContextVar("native_run_retry_budgets", default=None)
)

def _with_native_retry_budget_scope(operation: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
    """Give one runtime owner a retry scope inherited by its child tasks."""
    @wraps(operation)
    async def run(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        token = _NATIVE_RUN_RETRY_BUDGETS.set({})
        try:
            return await operation(*args, **kwargs)
        finally:
            _NATIVE_RUN_RETRY_BUDGETS.reset(token)

    return run

def _scoped_native_retry_budget(budget: RetryTimeBudget | None) -> RetryTimeBudget | None:
    scope = _NATIVE_RUN_RETRY_BUDGETS.get()
    if budget is None or scope is None:
        return budget
    key = id(budget)
    entry = scope.get(key)
    if entry is None:
        # Retain the original object so its identity stays unique within this
        # scope even when a temporary native client has already been closed.
        entry = (budget, RetryTimeBudget(budget.total_seconds))
        scope[key] = entry
    return entry[1]

async def with_retry(
    call_factory,
    max_retries: int | None,
    *,
    retry_time_budget: RetryTimeBudget | None = None,
    before_attempt: Callable[[], None] | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
) -> Any:
    """Retry transient provider errors with exponential backoff.

    Prioritizes Retry-After when available (OpenRouter/OpenAI-compatible).
    """
    attempt = 0
    while True:
        if before_attempt is not None:
            before_attempt()
        started = time.monotonic()
        try:
            return await call_factory()
        except BaseException as e:
            elapsed = time.monotonic() - started
            if on_failure is not None:
                on_failure(e)
            if not isinstance(e, Exception) or not is_retryable_error(e):
                raise
            retry_time_available = retry_time_budget is None or retry_time_budget.consume(
                elapsed
            )
            if max_retries is not None and attempt >= max_retries:
                raise
            if not retry_time_available:
                raise

            retry_after = extract_retry_after_seconds(e)
            base = retry_after if retry_after is not None else min(
                2.0 ** min(attempt, 30),
                MAX_EXPONENTIAL_RETRY_DELAY_SECONDS,
            )
            delay = max(0.0, base + random.uniform(0.0, RETRY_JITTER_MAX_SECONDS))
            if retry_time_budget is not None and not retry_time_budget.consume(delay):
                raise
            await asyncio.sleep(delay)
            attempt += 1

def _http_status_of(error: BaseException) -> int | None:
    """Read an SDK HTTP status from its direct attribute or response."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None

def is_retryable_error(error: Exception) -> bool:
    """Whether ``error`` looks transient (retryable status code or message)."""
    if isinstance(error, TransientProviderError):
        return True

    # The session handles context overflow by compacting before retry. Exclude
    # it here even when its message contains a transient-error fragment.
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
    gateway_refusal = False
    has_http_status = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        has_http_status = has_http_status or _http_status_of(current) is not None
        class_name = type(current).__name__.lower()
        if class_name in _TRANSIENT_TRANSPORT_CLASS_NAMES:
            return True
        message = str(current).lower()
        if class_name in _STATUSLESS_PROVIDER_ERROR_CLASS_NAMES and any(
            fragment in message for fragment in _TRANSIENT_GATEWAY_MESSAGE_FRAGMENTS
        ):
            gateway_refusal = True
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
    # A wrapper or cause carrying a status also excludes the gateway rule.
    return gateway_refusal and not has_http_status

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
