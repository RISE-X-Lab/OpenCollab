"""One bounded cgroup v2 per OS sandbox; control processes stay outside."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path


class SandboxCgroup:
    """Requires an administrator-delegated, empty parent with controllers enabled."""

    def __init__(self) -> None:
        configured = os.environ.get("EXECSERVER_CGROUP_ROOT")
        if not configured:
            raise RuntimeError("EXECSERVER_CGROUP_ROOT must name a delegated cgroup v2 parent")
        parent = Path(configured).resolve(strict=True)
        try:
            enabled = set((parent / "cgroup.subtree_control").read_text().split())
        except OSError as exc:
            raise RuntimeError("CGROUP parent is not a delegated cgroup v2 directory") from exc
        if not {"cpu", "memory", "pids"} <= enabled:
            raise RuntimeError("CGROUP parent must delegate cpu, memory and pids; refusing unbounded sandbox")
        values = {}
        for name, default in (("CPU_QUOTA_US", 100000), ("MEMORY_MAX_BYTES", 512 * 1024 * 1024),
                              ("PIDS_MAX", 128)):
            value = int(os.environ.get("EXECSERVER_" + name, str(default)))
            if value <= 0:
                raise ValueError(f"EXECSERVER_{name} must be positive")
            values[name] = value
        self.path = parent / ("sandbox-" + uuid.uuid4().hex)
        self.path.mkdir()
        try:
            (self.path / "cpu.max").write_text(f"{values['CPU_QUOTA_US']} 100000")
            (self.path / "memory.max").write_text(str(values["MEMORY_MAX_BYTES"]))
            (self.path / "memory.swap.max").write_text("0")
            (self.path / "memory.oom.group").write_text("1")
            (self.path / "pids.max").write_text(str(values["PIDS_MAX"]))
            if not (self.path / "cgroup.kill").exists():
                raise RuntimeError("CGROUP kernel must support cgroup.kill")
        except BaseException:
            self.path.rmdir()
            raise

    def wrap(self, argv: list[str]) -> list[str]:
        # Join before exec/unshare/fork so all payload descendants inherit the
        # limit. nsenter alone does not change cgroup membership.
        return ["/bin/sh", "-c", 'set -eu; printf "%s\\n" "$$" > "$1/cgroup.procs"; '
                'shift; exec "$@"', "cgroup-enter", str(self.path), *argv]

    def populated(self) -> bool:
        if not self.path.exists():
            return False
        return "populated 1" in (self.path / "cgroup.events").read_text().splitlines()

    def oom_kills(self) -> int:
        if not self.path.exists():
            return 0
        events = dict(line.split() for line in (self.path / "memory.events").read_text().splitlines())
        return int(events["oom_kill"])

    async def close(self, timeout: float = 5.0) -> None:
        if not self.path.exists():
            return
        (self.path / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + timeout
        while self.populated():
            if time.monotonic() >= deadline:
                raise RuntimeError("CGROUP kill did not reach populated=0; cleanup unverified")
            await asyncio.sleep(0.01)
        self.path.rmdir()
