"""Tracer — JSONL trajectory recorder for observability.

Pure Unix-style: append-write to a JSONL file. No databases, no heavy tracing
frameworks. Scientists can analyze trajectories with simple Python scripts.

Ref: tracer design with fine-grained steps for debugging agent failures.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from opencollab.adapters.safe_files import (
    ensure_directory_no_symlinks,
    open_regular_text_append,
    write_locked_text,
)

_TRACE_QUEUE_MAX_RECORDS = 1024
_CLOSE_WRITER = object()


@dataclass(slots=True)
class _FlushRequest:
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(slots=True)
class _TraceWriterState:
    handle: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_error: str | None = None
    dropped_steps: int = 0
    close_error: BaseException | None = None


def _latch_write_error(
    state: _TraceWriterState,
    exc: BaseException,
    *,
    dropped: int = 0,
) -> None:
    with state.lock:
        if state.write_error is None:
            state.write_error = f"{type(exc).__name__}: {exc}"
        state.dropped_steps += dropped


def _trace_writer_loop(
    records: queue.Queue[object],
    state: _TraceWriterState,
) -> None:
    while True:
        item = records.get()
        try:
            if item is _CLOSE_WRITER:
                try:
                    if not state.handle.closed:
                        state.handle.close()
                except BaseException as exc:
                    state.close_error = exc
                    _latch_write_error(state, exc)
                return
            if isinstance(item, _FlushRequest):
                try:
                    if not state.handle.closed:
                        state.handle.flush()
                except BaseException as exc:
                    _latch_write_error(state, exc)
                finally:
                    item.done.set()
                continue
            assert isinstance(item, str)
            with state.lock:
                failed = state.write_error is not None
                if failed:
                    state.dropped_steps += 1
            if failed:
                continue
            try:
                write_locked_text(state.handle, item)
            except BaseException as exc:
                _latch_write_error(state, exc, dropped=1)
                try:
                    state.handle.close()
                except BaseException:
                    pass
        finally:
            records.task_done()


class Tracer:
    """Transparent, append-only trajectory recorder (fail-soft Observer sink).

    Records every LLM call, tool execution, delegation, and compaction event
    with timestamps, token counts, and latencies.

    Fail-soft latch-and-drop: on the first write error it latches the error,
    closes the file, and thereafter *counts* dropped steps instead of raising —
    trajectory recording must never crash the run it observes, nor silently
    pretend a dropped step was written.

    Usage:
        tracer = Tracer("my-session")
        tracer.log_step("llm_call", {"model": "gpt-4o"}, tokens=1500, latency=2.3)
        tracer.flush()
    """

    def __init__(
        self,
        run_id: str,
        output_dir: str = "trajectories",
        *,
        filename: str | None = None,
    ):
        self.run_id = run_id
        self._output_dir = output_dir
        # ``filename`` decouples the on-disk name from ``run_id``: the workflow
        # run folder writes ``orchestration.jsonl`` while each record still
        # carries the meaningful ``run_id`` (e.g. an external task id). When
        # omitted the name falls back to ``<run_id>.jsonl`` (the prior behaviour).
        trace_filename = filename or f"{run_id}.jsonl"
        if (
            not trace_filename
            or trace_filename in {".", ".."}
            or os.path.basename(trace_filename) != trace_filename
            or "\0" in trace_filename
            or "\\" in trace_filename
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in trace_filename
            )
            or len(trace_filename.encode("utf-8", errors="surrogatepass")) > 255
        ):
            raise ValueError("trace filename must be one safe path component")
        self._path = os.path.join(output_dir, trace_filename)
        ensure_directory_no_symlinks(output_dir)
        self._file = open_regular_text_append(self._path)
        self._step_counter = 0
        self._state = _TraceWriterState(self._file)
        self._state_lock = self._state.lock
        self._lifecycle_lock = threading.Lock()
        self._records: queue.Queue[object] = queue.Queue(
            maxsize=_TRACE_QUEUE_MAX_RECORDS
        )
        self._closed = False
        self._writer = threading.Thread(
            target=_trace_writer_loop,
            args=(self._records, self._state),
            name=f"opencollab-tracer-{run_id}",
            daemon=True,
        )
        self._writer.start()

    def log_step(
        self,
        step_type: str,
        payload: dict[str, Any],
        tokens: int = 0,
        latency: float = 0.0,
    ) -> None:
        """Record a single step. step_type: llm_call | tool_exec | delegate | compaction | error."""
        with self._state_lock:
            self._step_counter += 1
            step = self._step_counter
        record = {
            "timestamp": time.time(),
            "step": step,
            "run_id": self.run_id,
            "type": step_type,
            "payload": payload,
            "metrics": {"tokens": tokens, "latency_s": round(latency, 4)},
        }
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        except Exception as exc:
            record["payload"] = {
                "serialization_error": type(exc).__name__,
                "payload_type": type(payload).__name__,
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._state_lock:
            if self._closed or self._state.write_error is not None:
                self._state.dropped_steps += 1
                return
            try:
                self._records.put_nowait(line)
            except queue.Full:
                self._state.write_error = "BufferError: trajectory queue is full"
                self._state.dropped_steps += 1

    def flush(self) -> None:
        """Force flush to disk."""
        with self._lifecycle_lock:
            self._records.join()
            with self._state_lock:
                if self._closed:
                    return
            request = _FlushRequest()
            self._records.put(request)
            request.done.wait()

    def close(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._closed:
                    error = self._state.close_error
                    if error is not None:
                        raise error
                    return
                self._closed = True
            self._records.put(_CLOSE_WRITER)
            self._writer.join()
            if self._state.close_error is not None:
                raise self._state.close_error

    @property
    def path(self) -> str:
        return self._path

    @property
    def write_error(self) -> str | None:
        with self._state_lock:
            return self._state.write_error

    @property
    def dropped_steps(self) -> int:
        with self._state_lock:
            return self._state.dropped_steps

    def __del__(self):
        try:
            self.close()
        except BaseException:
            # Python cannot propagate destructor failures to a caller. Explicit
            # close sites retain their ordinary error handling and reporting.
            pass
