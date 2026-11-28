"""Persist session snapshots after lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from contextvars import copy_context
from typing import Callable

from opencollab.application.ports import EventPublisherPort
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

SaveOperation = Callable[[], None]
PrepareSave = Callable[[], SaveOperation | None]

SAVE_TRIGGERS = frozenset(
    {"user_message_appended", "step_end", "budget_reserve_allocated"}
)

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
    """Persist consistent snapshots through one owned background worker.

    ``prepare_fn`` freezes mutable state on the event loop, then the worker
    runs file I/O in the default executor. Production saves append journal
    records and periodically compact them into an atomic base snapshot.
    Pending generations coalesce to the latest state while a write is active.

    The subscriber owns queued saves through caller cancellation. Callers must
    use a single subscriber per target to preserve write ordering.
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
                # Submit before a short-lived loop can cancel its remaining tasks.
                loop = asyncio.get_running_loop()
                write = loop.run_in_executor(None, copy_context().run, _run_save, operation)
                cancelled = await self._wait_owned(write) or cancelled
                error = write.result()
                if error is not None:
                    self._record_failure(error)
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _wait_owned(owner: asyncio.Future[object]) -> bool:
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
