"""Tracer — JSONL trajectory recorder for observability.

Pure Unix-style: append-write to a JSONL file. No databases, no heavy tracing
frameworks. Scientists can analyze trajectories with simple Python scripts.

Ref: tracer design with fine-grained steps for debugging agent failures.
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from typing import Any

from opencollab.adapters.safe_files import (
    ensure_directory_no_symlinks,
    open_regular_text_append,
    write_locked_text,
)


class Tracer:
    """Transparent, append-only trajectory recorder.

    Records every LLM call, tool execution, delegation, and compaction event
    with timestamps, token counts, and latencies.

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
        self._write_error: str | None = None
        self._dropped_steps = 0

    def log_step(
        self,
        step_type: str,
        payload: dict[str, Any],
        tokens: int = 0,
        latency: float = 0.0,
    ) -> None:
        """Record a single step. step_type: llm_call | tool_exec | delegate | compaction | error."""
        self._step_counter += 1
        record = {
            "timestamp": time.time(),
            "step": self._step_counter,
            "run_id": self.run_id,
            "type": step_type,
            "payload": payload,
            "metrics": {"tokens": tokens, "latency_s": round(latency, 4)},
        }
        if self._write_error is not None:
            self._dropped_steps += 1
            return
        try:
            write_locked_text(
                self._file,
                json.dumps(record, ensure_ascii=False) + "\n",
            )
        except Exception as exc:
            self._write_error = f"{type(exc).__name__}: {exc}"
            self._dropped_steps += 1
            try:
                self._file.close()
            except Exception:
                pass

    def flush(self) -> None:
        """Force flush to disk."""
        f = getattr(self, "_file", None)
        if f and not f.closed:
            try:
                f.flush()
            except Exception as exc:
                self._write_error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        f = getattr(self, "_file", None)
        if f and not f.closed:
            f.close()

    @property
    def path(self) -> str:
        return self._path

    @property
    def write_error(self) -> str | None:
        return self._write_error

    @property
    def dropped_steps(self) -> int:
        return self._dropped_steps

    def __del__(self):
        try:
            self.close()
        except BaseException:
            # Python cannot propagate destructor failures to a caller. Explicit
            # close sites retain their ordinary error handling and reporting.
            pass
