"""Stopped and failed children retain bounded diff delivery and trace evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application._scheduler_constants import WORKTREE_DIFF_MAX_CHARS
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from tests.runtime.worktree_test_support import _ChildSession, _Factory, _LeadSession


class _DiffEnvironment:
    def __init__(self, diff):
        self.diff = diff
        self.calls = 0

    async def get_diff(self):
        self.calls += 1
        return self.diff

    async def read_file(self, path):
        return "content\n"


class _FailedChild(_ChildSession):
    async def run_loop(self):
        raise RuntimeError("provider unavailable")


class _StoppedChild(_ChildSession):
    async def run_loop(self):
        self.state.set_phase(SessionPhase.STOPPED)
        self.state.terminal_reason = "step limit reached"
        return "stopped"


class _BrokenTracer:
    def log_step(self, **kwargs):
        raise OSError("trace unavailable")


@pytest.mark.parametrize("child_class", [_FailedChild, _StoppedChild])
@pytest.mark.parametrize("trace", ["off", "on", "broken"])
async def test_large_partial_diff_keeps_parent_bound_and_single_capture(tmp_path, child_class, trace):
    diff = (
        "diff --git a/solution.py b/solution.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/solution.py\n@@ -0,0 +1,10000 @@\n" + "+print(42)\n" * 10_000
    )
    env = _DiffEnvironment(diff)
    child = child_class("coder", "", env)
    lead = _LeadSession()
    tracer = Tracer(run_id="partial", output_dir=str(tmp_path)) if trace == "on" else None
    scheduler = Scheduler(
        session_factory=_Factory(child), worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None), tracer=_BrokenTracer() if trace == "broken" else tracer,
    )
    scheduler.register_lead(lead)
    try:
        aid = await scheduler.spawn(0, "coder", "implement", tool_call_id="child")
        row = PendingRow(tool_call_id="child", kind=RowKind.CHILD_AGENT, order=0, ref=aid)
        lead.state.pending_events.add(row)
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        await scheduler._tasks[aid]
        resume = scheduler._tasks.get(0)
        if resume is not None:
            await resume
        row = lead.state.pending_events.rows["child"]
        assert row.status is RowStatus.FAILED
        assert row.error.startswith("Error:")
        assert row.result.startswith(row.error)
        assert "[Changes made in worktree]" in row.result
        assert "chars truncated" in row.result
        assert len(row.result) < WORKTREE_DIFF_MAX_CHARS + 500
        assert scheduler.table.get(aid).result == row.result
        assert env.calls == 1
        if tracer is not None:
            tracer.flush()
            records = [json.loads(line) for line in Path(tracer.path).read_text().splitlines()]
            changes = [record["payload"] for record in records if record["type"] == "worktree_changes"]
            assert len(changes) == 1
            assert changes[0]["diff_chars"] == len(diff)
            assert changes[0]["truncated_in_result"] is True
            assert changes[0]["files"][0]["path"] == "solution.py"
    finally:
        if tracer is not None:
            tracer.close()
