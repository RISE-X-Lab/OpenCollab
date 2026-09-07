"""When the first token of a provider response arrived, or why nobody saw it.

``latency_s`` — the only per-call clock every artifact carries today — is the
wall time of a whole completion. Transport and generation are summed inside it
and cannot be separated afterwards: a 200-second call may be a gateway that
queued the request, or a model that wrote 4,000 tokens, and the recorded number
is the same either way. Two attempts on 2026-09-07 to infer "was this run's
instrument at fault?" from that number failed their positive controls for
exactly this reason.

Time to first token splits the interval. ``ttft_s`` covers connect, gateway
queueing and prefill; ``latency_s - ttft_s`` covers decode. The split is only
observable when the response is consumed as a stream — a non-streaming call
produces one event, at the end, and there is no first token to time. This
module therefore records the *absence* rather than inventing a value:
``ttft_measured`` is ``False``, ``ttft_s`` is ``None``, and
``ttft_unavailable_reason`` names the path. A recorded ``0.0`` means a first
chunk really did arrive inside the clock's resolution; ``None`` means nothing
watched for one. Downstream those must never read the same.

Scope of the numbers, because the outer ``latency_s`` and these do not cover the
same interval: every per-attempt field describes **the attempt whose result was
returned**. ``LLMClient.complete`` retries transient provider errors, and its
``latency_s`` includes every attempt and every backoff sleep; ``ttft_s`` and
``attempt_s`` do not. ``attempts`` is what makes the gap readable — when it is
1 the two clocks share a start, and when it is greater they do not.

The recorder is a context variable holding one mutable object. It is installed
once per ``LLMClient.complete`` and mutated by whichever provider module runs,
so no provider signature changes and no arm can take a path that skips it. A
child task inherits a copy of the context that still points at the same object,
so a stream drained inside ``asyncio.wait_for`` still reports into the call that
opened it.

Nothing here can change what a run does: it reads clocks and writes to its own
object. No request field, retry decision, timeout or budget is touched.
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
            reason = self.unavailable_reason or NOT_STREAMED
        return {
            "ttft_s": ttft,
            "ttft_measured": ttft is not None,
            "ttft_unavailable_reason": reason,
            "ttft_source": self.source,
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
