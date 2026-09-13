"""Running user turns behind an input line that never goes away.

A turn used to be the foreground: the REPL awaited it and there was nothing to
type into until it finished. Now the prompt is permanent, so a line submitted
mid-turn has to go somewhere — and that somewhere is a queue, not an interrupt.
Turns still run strictly one at a time, in the order they were typed, because
that is the order the agents' transcripts have to read in.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable


class TurnQueue:
    """Serialises user turns, and carries the interrupt for the running one."""

    def __init__(
        self,
        run_turn: Callable[[str, int, asyncio.Event], Awaitable[None]],
        *,
        on_depth: Callable[[int], None] | None = None,
    ) -> None:
        self._run_turn = run_turn
        self._on_depth = on_depth
        self._waiting: deque[tuple[str, int]] = deque()
        self._arrived = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._cancel: asyncio.Event | None = None

    @property
    def depth(self) -> int:
        """How many typed messages are waiting for their turn to run."""
        return len(self._waiting)

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def submit(self, line: str, aid: int) -> None:
        """Queue one user message for ``aid``."""
        self._waiting.append((line, aid))
        self._idle.clear()
        self._arrived.set()
        self._report_depth()

    def interrupt(self) -> bool:
        """Stop the running turn and drop what was queued behind it.

        Cooperative on purpose: the scheduler's ``cancel_event`` lets the agent
        stop at its next step boundary with its session settled and persisted,
        which cancelling the task cannot do. Returns whether there was anything
        to interrupt.
        """
        dropped = bool(self._waiting)
        self._waiting.clear()
        self._report_depth()
        cancel = self._cancel
        if cancel is not None and not cancel.is_set():
            cancel.set()
            return True
        return dropped

    async def drain(self) -> None:
        """Wait until nothing is queued and no turn is running."""
        await self._idle.wait()

    async def run(self) -> None:
        """Consume the queue until cancelled. Turn failures propagate."""
        while True:
            if not self._waiting:
                self._idle.set()
                self._arrived.clear()
                await self._arrived.wait()
                continue
            line, aid = self._waiting.popleft()
            self._report_depth()
            self._cancel = asyncio.Event()
            try:
                await self._run_turn(line, aid, self._cancel)
            finally:
                self._cancel = None

    def _report_depth(self) -> None:
        if self._on_depth is not None:
            self._on_depth(len(self._waiting))


__all__ = ["TurnQueue"]
