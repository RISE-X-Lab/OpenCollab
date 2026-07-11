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


class EventBus:
    """Fan-out broadcaster. Multiple subscribers; failures isolated per-sink."""

    def __init__(self, target: EventPublisherPort | EventCallback | None = None):
        self._targets: list[EventPublisherPort | EventCallback] = []
        if target is not None:
            self.subscribe(target)

    def subscribe(self, target: EventPublisherPort | EventCallback) -> None:
        self._targets.append(target)

    @property
    def sink(self) -> EventPublisherPort | EventCallback | None:
        """First subscribed target (for snapshot/build code that needs one)."""
        return self._targets[0] if self._targets else None

    @property
    def subscribers(self) -> tuple[EventPublisherPort | EventCallback, ...]:
        """Read-only view of every subscribed target, in subscription order."""
        return tuple(self._targets)

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


__all__ = ["EventBus", "EventCallback"]
