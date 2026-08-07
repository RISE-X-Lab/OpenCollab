"""Persist session snapshots after lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from opencollab.application.ports import EventPublisherPort
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

SaveOperation = Callable[[], None]
PrepareSave = Callable[[], SaveOperation | None]

SAVE_TRIGGERS = frozenset({"user_message_appended", "step_end"})

logger = logging.getLogger(__name__)


def _run_save(operation: SaveOperation) -> Exception | None:
    try:
        operation()
    except BaseException as exc:
        if isinstance(exc, Exception):
            return exc
        error = RuntimeError(f"auto-save raised {type(exc).__name__}: {exc}")
        error.__cause__ = exc
        return error
    return None


class AutoSaveSubscriber(EventPublisherPort):
    """Freeze-then-flush persistence of session snapshots (Command + Memento).

    Two phases keep the write off the hot path without tearing the snapshot:
    *freeze* runs ``prepare_fn`` on the event loop to capture a self-consistent
    copy of mutable session state, then *flush* runs the file I/O off-thread via
    ``asyncio.to_thread``. Step checkpoints use exponentially spaced
    generations (1, 2, 4, 8, ...) so the cumulative snapshot volume stays
    linear as a session grows. A single worker also coalesces generations that
    have not started yet, so a slow sink writes the latest pending snapshot
    instead of replaying every obsolete intermediate state.

    Ordering is guaranteed by *single-subscriber ownership*, not by any key:
    one subscriber owns every queued task, so caller cancellation cannot
    silently discard an already submitted snapshot, and callers must not create
    competing writers for the same target. Within-episode durability only —
    this is not cross-episode learning.
    """

    def __init__(
        self,
        save_fn: SaveOperation,
        *,
        prepare_fn: PrepareSave | None = None,
    ):
        self._save = save_fn
        self._prepare = prepare_fn
        self._tail: asyncio.Task[None] | None = None
        self._owners: set[asyncio.Task[None]] = set()
        self._last_error: Exception | None = None
        self._failure_count = 0
        self._pending = False
        self._step_generation = 0
        self._next_step_checkpoint = 1

    @property
    def last_error(self) -> Exception | None:
        """Most recent save error, retained after later successful saves."""
        return self._last_error

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """Queued or active saves that teardown must await."""
        return tuple(owner for owner in self._owners if not owner.done())

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS:
            return
        if event.type == "step_end":
            self._step_generation += 1
            if self._step_generation < self._next_step_checkpoint:
                return
            self._next_step_checkpoint *= 2
        self._enqueue()
        # Let the owned worker freeze the first pending generation before the
        # caller continues mutating session state, without waiting for I/O.
        await asyncio.sleep(0)

    def enqueue(self) -> asyncio.Task[None] | None:
        """Flush the latest generation through the owned persistence worker."""
        return self._enqueue()

    def _enqueue(self) -> asyncio.Task[None]:
        """Queue one generation, coalescing it behind an active write."""
        loop = asyncio.get_running_loop()
        self._pending = True
        owner = self._tail
        if owner is not None and owner.get_loop() is not loop:
            if not owner.done():
                error = RuntimeError("auto-save subscriber cannot span active event loops")
                self._record_failure(error)
                raise error
            owner = None
        if owner is not None and not owner.done():
            return owner
        owner = loop.create_task(self._drain_latest())
        self._tail = owner
        self._owners.add(owner)
        owner.add_done_callback(self._save_done)
        return owner

    def _prepare_operation(self) -> SaveOperation | None:
        if self._prepare is None:
            return self._save
        try:
            return self._prepare()
        except Exception as exc:
            self._record_failure(exc)
            return None

    async def _drain_latest(self) -> None:
        cancelled = False
        while self._pending:
            self._pending = False
            operation = self._prepare_operation()
            if operation is not None:
                write = asyncio.create_task(asyncio.to_thread(_run_save, operation))
                cancelled = await self._wait_owned(write) or cancelled
                error = write.result()
                if error is not None:
                    self._record_failure(error)
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _wait_owned(owner: asyncio.Task[object]) -> bool:
        """Finish one submitted save while preserving caller cancellation."""
        cancelled = False
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                if owner.done():
                    break
                cancelled = True
            except BaseException:
                break
        return cancelled

    def _record_failure(self, exc: Exception) -> None:
        self._last_error = exc
        self._failure_count += 1
        logger.warning("auto-save failed: %s", exc)

    def _save_done(self, owner: asyncio.Task[None]) -> None:
        self._owners.discard(owner)
        if self._tail is owner:
            self._tail = None
        if not owner.cancelled():
            owner.exception()
