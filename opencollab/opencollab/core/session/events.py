from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class SessionEvent:
    """Lightweight event emitted by Session-compatible loops."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[SessionEvent], Awaitable[None] | None]


class EventSink(Protocol):
    async def emit(self, event: SessionEvent) -> None:
        ...


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

    async def emit(self, event: SessionEvent) -> None:
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
