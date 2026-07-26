"""Shared fakes for WorkflowContext tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from opencollab.domain.session import TurnEnforcementState


class FakeState:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.turn = TurnEnforcementState()

class FakeSession:
    """A scripted one-shot session.

    ``reply`` is the run_loop() return value. ``tokens`` is reported via
    ``used_tokens``. ``gate`` (optional) lets a test hold run_loop() open to
    observe concurrency; ``started`` is set when run_loop() begins. ``boom``
    makes run_loop() raise to exercise error capture.
    """

    def __init__(
        self,
        *,
        reply: str = "done",
        tokens: int = 0,
        gate: asyncio.Event | None = None,
        boom: bool = False,
        on_enter: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.reply = reply
        self._tokens = tokens
        self._gate = gate
        self._boom = boom
        self._on_enter = on_enter
        self.state = FakeState()
        self.prompt: str | None = None

    async def add_user_message(self, content: str) -> None:
        self.prompt = content
        self.state.messages.append({"role": "user", "content": content})

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        if self._on_enter is not None:
            await self._on_enter()
        if self._gate is not None:
            await self._gate.wait()
        if self._boom:
            raise RuntimeError("agent exploded")
        return self.reply

    @property
    def used_tokens(self) -> int:
        return self._tokens

class CancelCleanupSession(FakeSession):
    def __init__(self, *, tokens_after_cancel: int = 0) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()
        self.started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self._tokens_after_cancel = tokens_after_cancel
        self._landed = False

    @property
    def used_tokens(self) -> int:
        return self._tokens_after_cancel if self._landed else 0

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        gate = asyncio.Event()
        self.started.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_cancel.wait()
            self._landed = True
            raise

class StubbornAddSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.add_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_add = asyncio.Event()
        self.run_loop_called = False

    async def add_user_message(self, content: str) -> None:
        self.add_started.set()
        while not self.release_add.is_set():
            try:
                await self.release_add.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
        await super().add_user_message(content)

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        self.run_loop_called = True
        return await super().run_loop(cancel_event)

class FakeFactory:
    """Hands out pre-scripted sessions in build order, recording build calls."""

    def __init__(self, sessions: Sequence[FakeSession]) -> None:
        self._sessions = list(sessions)
        self._idx = 0
        self.builds: list[dict[str, Any]] = []

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> FakeSession:
        self.builds.append(
            {
                "prompt": prompt,
                "budget": budget,
                "tools": tools,
                "isolation": isolation,
                "label": label,
                "tool_choice": tool_choice,
                "thinking": thinking,
            }
        )
        session = self._sessions[self._idx]
        self._idx += 1
        return session

class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

class _DeferredTokenSession(FakeSession):
    """A FakeSession whose reported token spend can be deferred.

    ``used_tokens`` reads 0 until ``land_spend()`` flips it to ``_tokens``. This
    models a concurrent agent whose spend lands AFTER another agent has passed
    agent()'s budget gate but BEFORE that agent computes its per-session budget —
    the exact window in which a naive ``int(remaining)`` would go negative.
    """

    def __init__(self, *, tokens: int, on_enter=None) -> None:
        super().__init__(reply="a", tokens=tokens, on_enter=on_enter)
        self._landed = False

    def land_spend(self) -> None:
        self._landed = True

    @property
    def used_tokens(self) -> int:
        return self._tokens if self._landed else 0

class FakeProbe:
    """A scripted WorkingTreeProbe recording how often it is asked.

    ``changed`` is the whole-tree answer. ``changed_excluding`` honors a
    ``{path: dirty}`` map when given (a path present in ``excludes`` whose only
    dirt is itself drops out): with no map it falls back to a scripted
    ``excluded_changed`` bool so tests can assert "tree dirty but source clean".
    """

    def __init__(
        self,
        *,
        changed: bool = True,
        boom: bool = False,
        excluded_changed: bool | None = None,
    ) -> None:
        self._changed = changed
        self._boom = boom
        self._excluded_changed = excluded_changed
        self.calls = 0
        self.exclude_calls: list[tuple[str, ...]] = []

    async def changed(self) -> bool:
        self.calls += 1
        if self._boom:
            raise RuntimeError("git unavailable")
        return self._changed

    async def changed_excluding(self, paths) -> bool:
        self.exclude_calls.append(tuple(paths))
        if self._boom:
            raise RuntimeError("git unavailable")
        if not paths:
            return self._changed
        if self._excluded_changed is not None:
            return self._excluded_changed
        return self._changed

    async def diff(self) -> str:
        if self._boom:
            raise RuntimeError("git unavailable")
        return "diff"
