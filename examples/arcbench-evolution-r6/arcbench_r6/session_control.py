"""Competition metric records for core-managed execution events."""

from __future__ import annotations

import json


class SessionMetrics:
    def __init__(self, workspace):
        self.path = workspace / ".arc/checks/session-metrics.jsonl"

    def event(self, phase, event):
        data = dict(event.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "phase": phase,
                        "event": event.type,
                        "run_id": event.run_id,
                        "session_id": event.session_id,
                        "tokens": event.used_tokens,
                        "steps": event.steps,
                        **data,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if event.type == "usage":
            with (self.path.parent / "wire-usage.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"phase": phase, "run_id": event.run_id, "session_id": event.session_id, **data})
                    + "\n"
                )
