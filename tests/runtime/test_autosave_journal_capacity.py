from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters import storage as storage_module
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.application.ports import SnapshotCapacityError, SnapshotSequenceConflict
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.tools import builtin_tools
from tests.support.session_run_test_support import llm_response, tool_call


@pytest.mark.parametrize("length", [2000, 8000])
async def test_current_tool_results_survive_autosave_capacity(tmp_path, monkeypatch, length):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", 32 * 1024)
    path = tmp_path / "review.json"
    for step in range(1, 4):
        (tmp_path / f"report-{step}.txt").write_text(f"Section {step}\n" + str(step) * length)

    reply_started = asyncio.Event()
    allow_completion = asyncio.Event()

    class RecordingModel:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            if self.calls == 4:
                reply_started.set()
                await allow_completion.wait()
                return llm_response(content="Review complete")
            call = tool_call(
                f"read-{self.calls}", "file_read",
                json.dumps({"path": f"report-{self.calls}.txt"}),
            )
            return llm_response(tool_calls=[call], finish_reason="tool_calls")

    environment = LocalEnvironment(str(tmp_path))
    session = build_session(
        agent=Agent(name="reviewer", system_prompt="Read and review the three local reports.",
                    tools=builtin_tools("file_read")),
        env=environment, llm=RecordingModel(), auto_save_path=str(path), max_steps=7,
    )
    running = None
    try:
        await session.add_user_message("Read each report and summarize the findings.")
        running = asyncio.create_task(session.run_loop())
        await asyncio.wait_for(reply_started.wait(), 5)
        snapshot = OpenCollab.read_session_snapshot(path)
        tool_ids = [row["tool_call_id"] for row in snapshot["messages"] if row["role"] == "tool"]
        live_ids = [row["tool_call_id"] for row in session.messages if row["role"] == "tool"]
        assert live_ids == tool_ids == ["read-1", "read-2", "read-3"]
        assert not session.persistence_errors
        allow_completion.set()
        assert await running == "Review complete"
    finally:
        allow_completion.set()
        if running is not None:
            if not reply_started.is_set():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await session.aclose()
        await environment.cleanup()


@pytest.mark.parametrize("rejected", ["capacity", "stale"])
def test_capacity_recovery_preserves_last_durable_state(tmp_path, monkeypatch, rejected):
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", 16 * 1024)
    path = tmp_path / "review.json"
    journal = path.with_suffix(".json.journal")
    store = SessionStore()
    base = [{"role": "system", "content": "Summarize the findings."}]
    first = {"role": "assistant", "content": "First note. " * 900}
    second = {"role": "assistant", "content": "Second note. " * 900}
    store.checkpoint_snapshot(str(path), base, meta={}, sequence=1)
    store.append_snapshot_delta(str(path), sequence=2, replace_from=1, messages=[first], meta={})
    before = path.read_bytes(), journal.read_bytes()
    if rejected == "capacity":
        error, sequence = SnapshotCapacityError, 3
    else:
        error, sequence = SnapshotSequenceConflict, 2
    with pytest.raises(error):
        store.append_snapshot_delta(
            str(path), sequence=sequence, replace_from=2, messages=[second], meta={},
        )
    assert (path.read_bytes(), journal.read_bytes()) == before
    assert store.load_snapshot(str(path), "")["messages"] == [*base, first]
