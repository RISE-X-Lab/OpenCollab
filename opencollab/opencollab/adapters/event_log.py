"""JSONL event sink for run-level observability."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from opencollab.adapters.safe_files import append_regular_text, ensure_directory_no_symlinks
from opencollab.application.ports import EventPublisherPort


class JsonlEventSink(EventPublisherPort):
    """Append events in emission order without changing agent behavior."""

    def __init__(self, path: str):
        self._path = path
        self._write_error: str | None = None
        self._initialization_error: str | None = None
        self._dropped_events = 0
        parent = os.path.dirname(path)
        if parent:
            try:
                ensure_directory_no_symlinks(parent)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._write_error = error
                self._initialization_error = error

    async def emit(self, event: Any) -> None:
        if self._initialization_error is not None:
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
            append_regular_text(
                self._path,
                json.dumps(payload, ensure_ascii=False, default=str) + "\n",
            )
        except Exception as exc:
            if self._write_error is None:
                self._write_error = f"{type(exc).__name__}: {exc}"
            self._dropped_events += 1

    @property
    def write_error(self) -> str | None:
        return self._write_error

    @property
    def dropped_events(self) -> int:
        return self._dropped_events


__all__ = ["JsonlEventSink"]
