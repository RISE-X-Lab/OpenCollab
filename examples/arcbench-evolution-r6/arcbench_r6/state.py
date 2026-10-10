"""Run-owned reports and cumulative usage across an explicit continuation."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from arc_light.reports import write_json


class RunState:
    def __init__(self, workspace: Path, task: str, *, run_id=None, resume=False, fresh=False):
        self.workspace = workspace
        self.directory = workspace / ".arc/checks"
        self.path = self.directory / "run-state.json"
        self.started = time.monotonic()
        self.elapsed_before = 0.0
        self._weave_state = None
        if resume:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if run_id is not None and self.data["run_id"] != run_id:
                raise ValueError("Continuation must use the original OC run_id")
            if (self.directory / "task.yaml").read_text(encoding="utf-8") != task:
                raise ValueError("Continuation requires the same original requirement document")
            self.elapsed_before = float(self.data.get("elapsed_seconds", 0))
        else:
            if self.directory.exists() and any(p.name != "outcome.json" for p in self.directory.iterdir()):
                if not fresh and (self.path.exists() or (self.directory / "evolution-baseline.json").exists()):
                    raise ValueError("Existing r6 run requires --resume, or an explicit --fresh input workspace")
                archive = workspace / ".arc/history"
                archive.mkdir(parents=True, exist_ok=True)
                destination = Path(tempfile.mkdtemp(prefix="previous-", dir=archive))
                shutil.move(str(self.directory), str(destination / "checks"))
                cache = destination / "checks/dependency-cache.json"
                if cache.is_file():
                    self.directory.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cache, self.directory / cache.name)
            self.directory.mkdir(parents=True, exist_ok=True)
            self.data = {
                "run_id": run_id or "arc-" + uuid.uuid4().hex,
                "sessions": {},
                "groups": {},
                "repair_rounds": [],
                "status": "running",
            }
            (self.directory / "task.yaml").write_text(task, encoding="utf-8")
        self.data["status"] = "running"
        self.persist()
        self.outcome(status="running", delivery_ok=False)

    @property
    def run_id(self):
        return self.data["run_id"]

    @property
    def tokens(self):
        if self._weave_state is not None:
            return self._weave_state.tokens
        return sum(row["tokens"] for row in self.data["sessions"].values())

    @property
    def elapsed(self):
        if self._weave_state is not None:
            return self._weave_state.elapsed
        return self.elapsed_before + time.monotonic() - self.started

    def bind_weave(self, state):
        self._weave_state = state

    def persist(self):
        self.data["elapsed_seconds"] = self.elapsed
        write_json(self.path, self.data)

    def outcome(self, **fields):
        result = {"run_id": self.run_id, "official_score": None, "total_tokens": self.tokens, **fields}
        write_json(self.directory / "outcome.json", result)
        return result

    def finish(self, **fields):
        self.data["status"] = fields.get("status", "completed")
        self.persist()
        return self.outcome(**fields)
