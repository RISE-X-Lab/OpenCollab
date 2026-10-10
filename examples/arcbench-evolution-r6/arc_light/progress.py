"""Small same-session reminder based on application content, including shell edits."""

from __future__ import annotations

import hashlib
from pathlib import Path


class _Progress:
    def __init__(self, workspace: Path, interval: int = 12, observer=None):
        self.workspace = workspace
        self.interval = interval
        self.previous = self.snapshot()
        self.unchanged = 0
        self.observer = observer

    def snapshot(self):
        contents = []
        for root in ("frontend/src", "backend/src"):
            base = self.workspace / root
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or {"node_modules", "__pycache__", "dist"}.intersection(path.parts):
                    continue
                if path.suffix not in {".js", ".jsx", ".ts", ".tsx", ".css", ".json", ".sql", ".html"}:
                    continue
                try:
                    contents.append(
                        (path.relative_to(self.workspace).as_posix(), hashlib.sha256(path.read_bytes()).digest())
                    )
                except OSError:
                    continue
        return contents

    def observe(self, name="", params=None, result=""):
        current = self.snapshot()
        old, new = dict(self.previous), dict(current)
        changed = [p for p in old.keys() | new.keys() if old.get(p) != new.get(p)]
        if self.observer:
            self.observer.observed(name, params or {}, result, changed)
        if current != self.previous:
            self.previous, self.unchanged = current, 0
            return ""
        self.unchanged += 1
        if self.unchanged % self.interval:
            return ""
        return (
            f"\n[Implementation progress: application source content has not changed for "
            f"{self.unchanged} tool calls. If this task still requires implementation, use "
            "the requirements already read to implement the next concrete UI/API/data path. "
            "Do not repeat a full specification dump or redesign. If checking a specific "
            "failure, continue that focused check. Use run_acceptance for shared entry or named "
            "requirement checks instead of writing another substitute test suite. "
            "This is a reminder, not an acceptance verdict.]"
        )


class _ObservedTool:
    def __init__(self, original, progress):
        self.original, self.progress = original, progress

    def __getattr__(self, key):
        # Preserve native timeout settings, schemas and execution-fact methods.
        return getattr(self.original, key)

    async def execute_with_runtime(self, params, runtime):
        if (
            self.progress.observer
            and self.progress.observer.acceptance_running
            and self.original.name in {"bash", "file_write", "apply_patch"}
        ):
            return "Coordinator check in progress. Wait for run_acceptance before edits or shell commands."
        key = getattr(runtime, "tool_call_id", None) or str(id(params))
        if self.progress.observer:
            self.progress.observer.active_tools[key] = self.original.name
        try:
            result = await self.original.execute_with_runtime(params, runtime)
            notice = self.progress.observe(self.original.name, params, result)
            return result + notice if isinstance(result, str) else result
        finally:
            if self.progress.observer:
                self.progress.observer.active_tools.pop(key, None)


def monitor_tools(workspace: Path, tools, observer=None):
    progress = _Progress(workspace, observer=observer)
    return [_ObservedTool(tool, progress) for tool in tools]
