"""Runtime event fan-out — implementation of EventPublisherPort.

The bus accepts any event object that carries ``type`` and ``data`` attributes
(structurally a DomainEvent). Subscribers may be either async callables or
objects with an async ``emit`` method. Per-subscriber failures are isolated so
one bad sink cannot break siblings or the loop.
"""

from __future__ import annotations

import asyncio
import inspect
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

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Live tasks owned by subscribers."""
        pending: list[asyncio.Task[Any]] = []
        seen: set[int] = set()
        for target in self._targets:
            tasks = getattr(target, "pending_tasks", ())
            for task in tasks:
                if not isinstance(task, asyncio.Task) or task.done() or id(task) in seen:
                    continue
                seen.add(id(task))
                pending.append(task)
        return tuple(pending)

    async def emit(self, event: Any) -> None:
        for target in self._targets:
            try:
                if hasattr(target, "emit"):
                    result = target.emit(event)  # type: ignore[union-attr]
                else:
                    result = target(event)  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception:
                # Subscriber failure must not break siblings or the loop.
                continue
            if not inspect.isawaitable(result):
                continue
            subscriber = asyncio.create_task(self._isolate_self_cancel(result))
            try:
                await asyncio.shield(subscriber)
            except asyncio.CancelledError:
                subscriber.cancel()
                await asyncio.gather(subscriber, return_exceptions=True)
                raise
            except Exception:
                continue

    @staticmethod
    async def _isolate_self_cancel(result: Awaitable[Any]) -> None:
        try:
            await result
        except asyncio.CancelledError:
            return


__all__ = ["EventBus", "EventCallback"]
