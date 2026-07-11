"""Tracer — JSONL trajectory recorder for observability.

Pure Unix-style: append-write to a JSONL file. No databases, no heavy tracing
frameworks. Scientists can analyze trajectories with simple Python scripts.

Ref:
- Design doc: Tracer class with log_step()
- Harness Engineering: fine-grained trajectory tracking for debugging agent failures
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


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
        os.makedirs(output_dir, exist_ok=True)
        # ``filename`` decouples the on-disk name from ``run_id``: the workflow
        # run folder writes ``orchestration.jsonl`` while each record still
        # carries the meaningful ``run_id`` (e.g. the SWE-bench task id). When
        # omitted the name falls back to ``<run_id>.jsonl`` (the prior behaviour).
        self._path = os.path.join(output_dir, filename or f"{run_id}.jsonl")
        self._file = open(self._path, "a", encoding="utf-8")
        self._step_counter = 0

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
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def flush(self) -> None:
        """Force flush to disk."""
        f = getattr(self, "_file", None)
        if f and not f.closed:
            f.flush()

    def close(self) -> None:
        f = getattr(self, "_file", None)
        if f and not f.closed:
            f.close()

    @property
    def path(self) -> str:
        return self._path

    def __del__(self):
        self.close()
