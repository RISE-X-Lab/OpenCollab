"""JSONL event sink for run-level observability."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from opencollab.adapters.safe_files import (
    append_regular_text,
    ensure_directory_no_symlinks,
    regular_path_identity,
)
from opencollab.application.ports import EventPublisherPort

EVENT_IO_TIMEOUT_SECONDS = 1.0


def _append_event_record(
    path: str,
    line: str,
    previous_identity: tuple[int, int, int, int, int] | None,
) -> tuple[int, int, int, int, int]:
    if previous_identity is not None:
        current_identity = regular_path_identity(path)
        previous_device, previous_inode, previous_size, previous_mtime, previous_ctime = (
            previous_identity
        )
        if current_identity[:2] != (previous_device, previous_inode):
            raise OSError("event log path changed between records")
        if current_identity[2] < previous_size:
            raise OSError("event log was truncated between records")
        if current_identity[2:] != (
            previous_size,
            previous_mtime,
            previous_ctime,
        ):
            raise OSError("event log was modified between records")
    append_regular_text(path, line)
    return regular_path_identity(path)


class _DaemonIoWorker:
    """One serial daemon owner so blocked observability I/O cannot hold shutdown."""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[
            tuple[
                Callable[..., Any],
                tuple[Any, ...],
                asyncio.AbstractEventLoop,
                asyncio.Future[Any],
            ]
        ] = queue.SimpleQueue()
        self._started = False
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            threading.Thread(
                target=self._run,
                name="opencollab-event-log-io",
                daemon=True,
            ).start()
            self._started = True

    def _run(self) -> None:
        while True:
            function, args, loop, future = self._jobs.get()
            try:
                outcome = function(*args)
            except BaseException as exc:
                self._deliver(loop, future, exception=exc)
            else:
                self._deliver(loop, future, result=outcome)

    @staticmethod
    def _deliver(
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[Any],
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        def finish() -> None:
            if future.done():
                return
            if exception is None:
                future.set_result(result)
            else:
                future.set_exception(exception)

        try:
            loop.call_soon_threadsafe(finish)
        except RuntimeError:
            pass

    async def call(self, function: Callable[..., Any], *args: Any) -> Any:
        self._ensure_started()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._jobs.put((function, args, loop, future))
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=EVENT_IO_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            future.cancel()
            raise


class JsonlEventSink(EventPublisherPort):
    """Append runtime/scheduler events to a JSONL file.

    This sink is intentionally passive: write failures are swallowed so
    observability never changes the agent's behavior.
    """

    def __init__(self, path: str):
        self._path = path
        self._write_error: str | None = None
        self._initialization_error: str | None = None
        self._dropped_events = 0
        self._last_file_identity: tuple[int, int, int, int, int] | None = None
        self._continuity_broken = False
        self._emit_lock = asyncio.Lock()
        self._io_worker = _DaemonIoWorker()
        parent = os.path.dirname(path)
        if parent:
            try:
                ensure_directory_no_symlinks(parent)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._write_error = error
                self._initialization_error = error

    async def emit(self, event: Any) -> None:
        async with self._emit_lock:
            if self._initialization_error is not None or self._continuity_broken:
                self._dropped_events += 1
                return
            try:
                if is_dataclass(event):
                    payload = asdict(event)
                else:
                    payload = {
                        "type": getattr(event, "type", type(event).__name__),
                        "data": getattr(event, "data", {}),
                    }
                payload.setdefault("timestamp", time.time())
                line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
                self._last_file_identity = await self._io_worker.call(
                    _append_event_record,
                    self._path,
                    line,
                    self._last_file_identity,
                )
            except Exception as exc:
                if self._write_error is None:
                    self._write_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, asyncio.TimeoutError) or any(
                    marker in str(exc)
                    for marker in (
                        "changed while writing",
                        "changed between records",
                        "truncated between records",
                        "modified between records",
                    )
                ):
                    self._continuity_broken = True
                self._dropped_events += 1
            return

    @property
    def write_error(self) -> str | None:
        return self._write_error

    @property
    def dropped_events(self) -> int:
        return self._dropped_events


__all__ = ["JsonlEventSink"]
