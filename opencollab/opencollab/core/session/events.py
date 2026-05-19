"""Session event sink/bus.

The event value type now lives in ``opencollab.domain.events``. This module
re-exports it as ``SessionEvent`` for backward compatibility — every existing
``from opencollab.core.session import SessionEvent`` import keeps resolving to
the same dataclass.

The bus accepts either ``SessionRuntimeEvent`` or ``TeamEvent`` (both are
duck-compatible: they carry ``type`` and ``data`` attributes), so team
orchestration and the session run loop share one fan-out channel during
the Step12 migration.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol

from opencollab.domain.events import SessionRuntimeEvent as _SessionRuntimeEvent
from opencollab.domain.events import TeamEvent as _TeamEvent


# Backward-compatible alias. Production code should prefer SessionRuntimeEvent
# from opencollab.domain.events for new call sites.
SessionEvent = _SessionRuntimeEvent
SessionRuntimeEvent = _SessionRuntimeEvent
TeamEvent = _TeamEvent


EventCallback = Callable[[Any], Awaitable[None] | None]


class EventSink(Protocol):
    async def emit(self, event: Any) -> None:
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


__all__ = [
    "EventBus",
    "EventCallback",
    "EventSink",
    "SessionEvent",
    "SessionRuntimeEvent",
    "TeamEvent",
]
