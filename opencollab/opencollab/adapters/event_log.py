"""JSONL event sink for run-level observability."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from opencollab.application.ports import EventPublisherPort


class JsonlEventSink(EventPublisherPort):
    """Append runtime/scheduler events to a JSONL file.

    This sink is intentionally passive: write failures are swallowed so
    observability never changes the agent's behavior.
    """

    def __init__(self, path: str):
        self._path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def emit(self, event: Any) -> None:
        try:
            if is_dataclass(event):
                payload = asdict(event)
            else:
                payload = {
                    "type": getattr(event, "type", type(event).__name__),
                    "data": getattr(event, "data", {}),
                }
            payload.setdefault("timestamp", time.time())
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            return


__all__ = ["JsonlEventSink"]
