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
    """Freeze and save snapshots in submission order on one event loop.

    ``prepare_fn`` captures mutable session state before the save task is
    scheduled. The subscriber owns every queued task so caller cancellation
    cannot silently discard an already submitted snapshot.
    """

    def __init__(
        self,
        save_fn: SaveOperation,
        *,
        prepare_fn: PrepareSave | None = None,
        serialization_key: str | None = None,
    ):
        self._save = save_fn
        self._prepare = prepare_fn
        # Kept as an accepted argument for SDK compatibility. Ordering belongs
        # to one subscriber; callers must not create competing writers.
        self._serialization_key = serialization_key
        self._tail: asyncio.Task[None] | None = None
        self._owners: set[asyncio.Task[None]] = set()
        self._last_error: Exception | None = None
        self._failure_count = 0

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
        owner = self.enqueue()
        if owner is not None:
            await asyncio.shield(owner)

    def enqueue(self) -> asyncio.Task[None] | None:
        """Freeze and queue one save without waiting for earlier snapshots."""
        operation = self._prepare_operation()
        if operation is None:
            return None
        loop = asyncio.get_running_loop()
        previous = self._tail
        if previous is not None and previous.get_loop() is not loop:
            if not previous.done():
                error = RuntimeError("auto-save subscriber cannot span active event loops")
                self._record_failure(error)
                raise error
            previous = None
        owner = loop.create_task(self._save_after(previous, operation))
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

    async def _save_after(
        self,
        previous: asyncio.Task[None] | None,
        operation: SaveOperation,
    ) -> None:
        cancelled = False
        if previous is not None:
            cancelled = await self._wait_owned(previous)
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
