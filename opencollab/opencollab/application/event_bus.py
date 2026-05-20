"""Runtime event fan-out — implementation of EventPublisherPort.

The bus accepts any event object that carries ``type`` and ``data`` attributes
(structurally a DomainEvent). Subscribers may be either async callables or
objects with an async ``emit`` method. Per-subscriber failures are isolated so
one bad sink cannot break siblings or the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from opencollab.application.ports import EventPublisherPort


EventCallback = Callable[[Any], Awaitable[None] | None]

# EventSink is the subscriber-side name for the same shape EventPublisherPort
# describes (an object with ``async emit(event)``). Kept as an alias so older
# call sites that import ``EventSink`` continue to work, while new code can
# implement ``EventPublisherPort`` directly to match the target diagram.
EventSink = EventPublisherPort


class EventBus:
    """Fan-out broadcaster. Multiple subscribers; failures isolated per-sink."""

    def __init__(self, target: EventSink | EventCallback | None = None):
        self._targets: list[EventSink | EventCallback] = []
        if target is not None:
            self.subscribe(target)

    def subscribe(self, target: EventSink | EventCallback) -> None:
        self._targets.append(target)

    @property
    def sink(self) -> EventSink | EventCallback | None:
        """First subscribed target (for snapshot/build code that needs one)."""
        return self._targets[0] if self._targets else None

    async def emit(self, event: Any) -> None:
        for target in self._targets:
            try:
                if hasattr(target, "emit"):
                    result = target.emit(event)  # type: ignore[union-attr]
                else:
                    result = target(event)  # type: ignore[operator]
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # Subscriber failure must not break siblings or the loop.
                continue


__all__ = ["EventBus", "EventCallback", "EventSink"]
