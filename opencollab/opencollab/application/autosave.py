"""AutoSaveSubscriber — persists session messages on lifecycle events.

Listens for the events that change persisted state:
- `user_message_appended` — user added a turn
- `step_end`             — assistant finished a step

Save failures stay isolated from sibling subscribers and remain observable on
the subscriber. Saves run outside the event-loop thread and retain submission
order even when the event callback awaiting a save is cancelled.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Callable

from opencollab.application.ports import EventPublisherPort
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

SaveOperation = Callable[[], None]
PrepareSave = Callable[[], SaveOperation | None]

SAVE_TRIGGERS = frozenset({
    "user_message_appended",
    "step_end",
})

logger = logging.getLogger(__name__)

MAX_CANCELLED_SAVE_WAIT_SECONDS = 0.25
MAX_DAEMON_SAVE_WORKERS = 32
_SAVE_WORKER_SLOTS = threading.BoundedSemaphore(MAX_DAEMON_SAVE_WORKERS)


def _submit_daemon_save(
    operation: SaveOperation,
) -> concurrent.futures.Future[None]:
    """Run blocking persistence without joining its thread at loop shutdown."""
    future: concurrent.futures.Future[None] = concurrent.futures.Future()
    if not _SAVE_WORKER_SLOTS.acquire(blocking=False):
        future.set_exception(
            RuntimeError(
                f"auto-save worker limit of {MAX_DAEMON_SAVE_WORKERS} is exhausted"
            )
        )
        return future

    def worker() -> None:
        error: BaseException | None = None
        try:
            operation()
        except BaseException as exc:
            error = exc
        finally:
            _SAVE_WORKER_SLOTS.release()
        if error is None:
            future.set_result(None)
        else:
            if isinstance(error, Exception):
                future.set_exception(error)
            else:
                wrapped = RuntimeError(
                    "auto-save daemon raised "
                    f"{type(error).__name__}: {error}"
                )
                wrapped.__cause__ = error
                future.set_exception(wrapped)

    try:
        threading.Thread(
            target=worker,
            name="opencollab-auto-save",
            daemon=True,
        ).start()
    except BaseException as exc:
        _SAVE_WORKER_SLOTS.release()
        if isinstance(exc, Exception):
            future.set_exception(exc)
        else:
            wrapped = RuntimeError(
                "auto-save worker startup raised "
                f"{type(exc).__name__}: {exc}"
            )
            wrapped.__cause__ = exc
            future.set_exception(wrapped)
    return future


class AutoSaveSubscriber(EventPublisherPort):
    def __init__(
        self,
        save_fn: SaveOperation,
        *,
        prepare_fn: PrepareSave | None = None,
    ):
        self._save = save_fn
        self._prepare = prepare_fn
        self._tail: asyncio.Task[None] | None = None
        self._write_tail: concurrent.futures.Future[None] | None = None
        self._owners: set[asyncio.Task[None]] = set()
        self._last_error: Exception | None = None
        self._failure_count = 0
        self._failure_lock = threading.Lock()

    @property
    def last_error(self) -> Exception | None:
        """Most recent save error, retained after later successful saves."""
        with self._failure_lock:
            return self._last_error

    @property
    def failure_count(self) -> int:
        with self._failure_lock:
            return self._failure_count

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """All live save owners, including a caller-cancelled callback's owner."""
        return tuple(owner for owner in self._owners if not owner.done())

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS:
            return
        owner = self.enqueue()
        if owner is not None:
            await asyncio.shield(owner)

    def enqueue(self) -> asyncio.Task[None] | None:
        """Freeze and queue one save without waiting for its file I/O."""
        operation = self._save
        if self._prepare is not None:
            try:
                prepared = self._prepare()
            except Exception as exc:
                self._record_failure(exc)
                return
            if prepared is None:
                return
            operation = prepared
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._record_failure(exc)
            raise
        previous = self._tail
        if previous is not None and previous.get_loop() is not loop:
            if not previous.done():
                self._record_failure(
                    RuntimeError(
                        "auto-save owner was abandoned with its original event loop"
                    )
                )
            previous = None
        owner = loop.create_task(self._save_after(previous, operation))
        self._tail = owner
        self._owners.add(owner)
        owner.add_done_callback(self._save_done)
        return owner

    async def _save_after(
        self,
        previous: asyncio.Task[None] | None,
        operation: SaveOperation,
    ) -> None:
        cancelled = False
        cancel_deadline: float | None = None
        loop = asyncio.get_running_loop()

        def mark_cancelled() -> None:
            nonlocal cancelled, cancel_deadline
            cancelled = True
            if cancel_deadline is None:
                cancel_deadline = loop.time() + MAX_CANCELLED_SAVE_WAIT_SECONDS

        async def wait_for_owned(
            future: asyncio.Future[object]
            | concurrent.futures.Future[None],
        ) -> bool:
            nonlocal cancelled, cancel_deadline
            if isinstance(future, concurrent.futures.Future):
                awaitable: asyncio.Future[object] = asyncio.wrap_future(
                    future,
                    loop=loop,
                )
            else:
                if future.get_loop() is not loop:
                    if future.done():
                        return True
                    raise RuntimeError(
                        "cannot await a live auto-save owner from another event loop"
                    )
                awaitable = future
            while not awaitable.done():
                try:
                    if cancel_deadline is None:
                        await asyncio.shield(awaitable)
                    else:
                        remaining = cancel_deadline - loop.time()
                        if remaining <= 0:
                            return False
                        _done, pending = await asyncio.wait(
                            {awaitable},
                            timeout=remaining,
                        )
                        if pending:
                            return False
                except asyncio.CancelledError:
                    mark_cancelled()
                except Exception:
                    if awaitable.done():
                        # The result is retrieved by the caller or worker callback.
                        return True
                    raise
            return True

        def owner_timed_out() -> None:
            self._record_failure(
                TimeoutError(
                    "cancelled auto-save owner exceeded its final wait deadline; "
                    "the daemon worker remains serialized ahead of newer saves"
                )
            )

        if previous is not None:
            if not await wait_for_owned(previous):
                owner_timed_out()
                raise asyncio.CancelledError
            try:
                previous.result()
            except asyncio.CancelledError:
                mark_cancelled()
            except Exception:
                # A preceding owner's persistence error is already sticky on
                # this subscriber. Preserve ordering and write this snapshot.
                pass

        # An owner may reach its deadline before a blocking daemon returns. Keep
        # the actual worker future as the serialization fence: a newer snapshot
        # can only start after the old operation really exits, so a late old
        # write can never overwrite a newer committed snapshot.
        previous_write = self._write_tail
        if previous_write is not None and not previous_write.done():
            if not await wait_for_owned(previous_write):
                owner_timed_out()
                raise asyncio.CancelledError

        write = _submit_daemon_save(operation)
        self._write_tail = write
        write.add_done_callback(self._write_done)
        if not await wait_for_owned(write):
            owner_timed_out()
            raise asyncio.CancelledError
        try:
            write.result()
        except BaseException:
            # _write_done records the sticky persistence failure.
            pass
        if cancelled:
            raise asyncio.CancelledError

    def _write_done(self, write: concurrent.futures.Future[None]) -> None:
        if write.cancelled():
            self._record_failure(
                RuntimeError("auto-save daemon result was cancelled")
            )
            return
        error = write.exception()
        if isinstance(error, Exception):
            self._record_failure(error)
        elif error is not None:
            self._record_failure(RuntimeError(f"auto-save daemon failed: {error}"))

    def _record_failure(self, exc: Exception) -> None:
        with self._failure_lock:
            self._last_error = exc
            self._failure_count += 1
        logger.warning("auto-save failed: %s", exc)

    def _save_done(self, owner: asyncio.Task[None]) -> None:
        self._owners.discard(owner)
        if self._tail is owner:
            self._tail = None
        if owner.cancelled():
            return
        # Save exceptions are recorded by ``_save_after``. Retrieve any
        # unexpected task exception so an abandoned, caller-cancelled emit does
        # not produce an unhandled-task warning.
        owner.exception()
