from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol

from opencollab.application.ports import AskUserPort, EventPublisherPort, PermissionPort


class SuspendableRender(Protocol):
    """A render target whose live output can be paused for user input."""

    def suspend_live(self) -> bool: ...
    def resume_live(self, was_suspended: bool) -> None: ...


class TuiEventSink(EventPublisherPort):
    """Bus subscriber that accepts both runtime and team event families.

    The dispatch lives inside the TUI itself (see ``adapters.tui.renderer.TUI.event_handler``);
    this sink is the bridge from the async event bus to the synchronous
    TUI handler.
    """

    def __init__(self, tui):
        self.tui = tui

    async def emit(self, event: Any) -> None:
        result = self.tui.event_handler(event)
        if asyncio.iscoroutine(result):
            await result


class TuiPermissionPolicy(PermissionPort):
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


class TuiAskUserPolicy(AskUserPort):
    """Ask policy that pauses a live render around a free-text prompt.

    Reuses the same suspend/resume seam as ``TuiPermissionPolicy`` so the
    ``ask_user`` tool's question is not clobbered by the Rich Live frame. On
    EOF/interrupt it raises ``EOFError`` so the tool reports the user declined.
    """

    def __init__(
        self,
        render: SuspendableRender,
        read_line: Callable[[str], Awaitable[str]],
    ):
        self._render = render
        self._read_line = read_line

    async def ask(self, question: str) -> str:
        was_suspended = self._render.suspend_live()
        try:
            return await self._read_line(f"[Agent asks] {question}\n> ")
        finally:
            self._render.resume_live(was_suspended)
