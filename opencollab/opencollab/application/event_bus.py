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
        self._pending_tasks: set[asyncio.Task[Any]] = set()
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
        """Live tasks owned by subscribers after their callback waiter exits."""
        pending: list[asyncio.Task[Any]] = []
        seen: set[int] = set()
        for task in self._pending_tasks:
            if task.done() or id(task) in seen:
                continue
            seen.add(id(task))
            pending.append(task)
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
                continue
            except Exception:
                # Subscriber failure must not break siblings or the loop.
                continue
            if not inspect.isawaitable(result):
                continue
            owner = asyncio.ensure_future(result)
            self._pending_tasks.add(owner)
            owner.add_done_callback(self._pending_tasks.discard)
            try:
                await asyncio.wait({owner})
            except asyncio.CancelledError:
                owner.cancel()
                owner.add_done_callback(_consume_task_result)
                raise
            try:
                owner.result()
            except asyncio.CancelledError:
                # A subscriber that cancels itself is an isolated sink failure.
                continue
            except Exception:
                continue


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


__all__ = ["EventBus", "EventCallback"]
