"""Per-attempt transport timing for the first protocol event.

The legacy ``ttft_s`` and ``first_token_at`` fields measure arrival of the first
Chat chunk or Responses event, including role-only and response.created events.
``ttft_semantics`` identifies that measurement explicitly. Generated text can
arrive later, so subtracting this value from latency does not measure decoding.
The outer latency includes retries and backoff; these clocks cover the last
attempt. Non-streamed requests and streams without an event carry distinct
unavailable reasons.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: Why a call has no TTFT. One reason today: the response was not streamed.
NOT_STREAMED = "response_not_streamed"

#: No provider attempt ever started, so there is nothing to say about one —
#: a request rejected during validation, before any socket was opened.
NO_ATTEMPT = "no_provider_attempt_started"

#: Sources, so a reader can tell which wire format produced the measurement
#: rather than assuming every stream reports the same event.
CHAT_STREAM = "chat_stream_first_chunk"
RESPONSES_STREAM = "responses_stream_first_event"

#: Exactly the keys ``snapshot`` writes. Tests assert against this so a key
#: added here without a decision downstream shows up as a failure, not as a
#: field nobody reads.
TRANSPORT_TIMING_KEYS: tuple[str, ...] = (
    "ttft_s",
    "ttft_measured",
    "ttft_unavailable_reason",
    "ttft_source",
    "ttft_semantics",
    "streamed",
    "request_started_at",
    "first_token_at",
    "attempt_s",
    "attempts",
)


@dataclass
class FirstTokenTimer:
    """Timing for one ``LLMClient.complete``, rewritten by each attempt."""

    attempts: int = 0
    streamed: bool | None = None
    unavailable_reason: str | None = None
    source: str | None = None
    _started_monotonic: float | None = None
    _started_wall: float | None = None
    _first_token_monotonic: float | None = None
    _first_token_wall: float | None = None

    def begin_attempt(self, *, streamed: bool, unavailable_reason: str | None) -> None:
        """One provider request is about to go out; forget the previous one.

        A retried call must not inherit the first-token time of the attempt that
        broke: that number would describe a response nobody received.
        """
        self.attempts += 1
        self.streamed = streamed
        self.unavailable_reason = unavailable_reason
        self.source = None
        self._first_token_monotonic = None
        self._first_token_wall = None
        self._started_monotonic = time.monotonic()
        self._started_wall = time.time()

    def mark_first_token(self, source: str) -> None:
        """The first chunk of the current attempt landed. Later ones are not it."""
        if self._first_token_monotonic is not None:
            return
        self._first_token_monotonic = time.monotonic()
        self._first_token_wall = time.time()
        self.source = source

    def snapshot(self) -> dict[str, Any]:
        """The record for the attempt that produced the returned result."""
        ttft: float | None = None
        if self._started_monotonic is not None and self._first_token_monotonic is not None:
            ttft = round(max(self._first_token_monotonic - self._started_monotonic, 0.0), 4)
        attempt_s: float | None = None
        if self._started_monotonic is not None:
            attempt_s = round(max(time.monotonic() - self._started_monotonic, 0.0), 4)
        if ttft is not None:
            reason = None
        elif self.attempts == 0:
            reason = NO_ATTEMPT
        else:
            reason = self.unavailable_reason or ("stream_received_no_events" if self.streamed else NOT_STREAMED)
        return {
            "ttft_s": ttft,
            "ttft_measured": ttft is not None,
            "ttft_unavailable_reason": reason,
            "ttft_source": self.source,
            "ttft_semantics": "first_protocol_event",
            "streamed": self.streamed,
            "request_started_at": (
                round(self._started_wall, 6) if self._started_wall is not None else None
            ),
            "first_token_at": (
                round(self._first_token_wall, 6) if self._first_token_wall is not None else None
            ),
            "attempt_s": attempt_s,
            "attempts": self.attempts,
        }


_CURRENT: ContextVar[FirstTokenTimer | None] = ContextVar(
    "opencollab_llm_first_token_timer", default=None
)


@contextmanager
def recording() -> Iterator[FirstTokenTimer]:
    """Install a timer for one completion and take it back down afterwards."""
    timer = FirstTokenTimer()
    token = _CURRENT.set(timer)
    try:
        yield timer
    finally:
        _CURRENT.reset(token)


def begin_attempt(*, streamed: bool, unavailable_reason: str | None = None) -> None:
    """Stamp the moment one provider request goes out. No-op outside a call."""
    timer = _CURRENT.get()
    if timer is not None:
        timer.begin_attempt(streamed=streamed, unavailable_reason=unavailable_reason)


def mark_first_token(source: str) -> None:
    """Stamp the arrival of the current attempt's first chunk or event."""
    timer = _CURRENT.get()
    if timer is not None:
        timer.mark_first_token(source)


__all__ = [
    "CHAT_STREAM",
    "NOT_STREAMED",
    "NO_ATTEMPT",
    "RESPONSES_STREAM",
    "TRANSPORT_TIMING_KEYS",
    "FirstTokenTimer",
    "begin_attempt",
    "mark_first_token",
    "recording",
]
