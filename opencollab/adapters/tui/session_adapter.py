from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from opencollab.application.ports import AskUserPort, EventPublisherPort, PermissionPort


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
    """Permission policy that asks its y/N through the terminal's one input line.

    Nothing is suspended around the question any more: the prompt owns the
    bottom of the screen for the whole session, so an agent asking for
    permission is one more thing that input line is asked to read.
    """

    def __init__(self, read_line: Callable[[str], Awaitable[str]]):
        self._read_line = read_line

    async def confirm(self, prompt: str) -> bool:
        try:
            answer = await self._read_line(f"{prompt} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")


class TuiAskUserPolicy(AskUserPort):
    """Ask policy that reads a free-text answer from the same input line.

    On EOF/interrupt it raises ``EOFError`` so the tool reports the user
    declined.
    """

    def __init__(self, read_line: Callable[[str], Awaitable[str]]):
        self._read_line = read_line

    async def ask(self, question: str) -> str:
        return await self._read_line(f"[Agent asks] {question}\n> ")
