from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from opencollab.core.session import EventSink, PermissionPolicy, SessionEvent


class SuspendableRender(Protocol):
    """A render target whose live output can be paused for user input."""

    def suspend_live(self) -> bool: ...
    def resume_live(self, was_suspended: bool) -> None: ...


class TuiEventSink(EventSink):
    def __init__(self, tui):
        self.tui = tui

    async def emit(self, event: SessionEvent) -> None:
        result = self.tui.event_handler(event)
        if asyncio.iscoroutine(result):
            await result


class TuiPermissionPolicy(PermissionPolicy):
    """Permission policy that pauses a live render around the y/N prompt."""

    def __init__(
        self,
        render: SuspendableRender,
        read_line: Callable[[str], Awaitable[str]],
    ):
        self._render = render
        self._read_line = read_line

    async def confirm(self, prompt: str) -> bool:
        was_suspended = self._render.suspend_live()
        try:
            answer = await self._read_line(f"{prompt} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            self._render.resume_live(was_suspended)
        return answer.strip().lower() in ("y", "yes")
