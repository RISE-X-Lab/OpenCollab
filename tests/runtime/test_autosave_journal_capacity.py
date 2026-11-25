from __future__ import annotations

import asyncio
import errno
import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

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


_ROLLOVER_LIMIT = 16 * 1024
_SYSTEM_MESSAGE = {"role": "system", "content": "Keep the latest review draft."}


def _draft(number: int) -> dict[str, str]:
    return {"role": "user", "content": f"Draft {number} " + "text " * 1200}


def _append_draft(path: str, sequence: int, number: int) -> None:
    SessionStore().append_snapshot_delta(
        path, sequence=sequence, replace_from=1, messages=[_draft(number)],
        meta={"session_state": {"draft": number}},
        seen_result_hashes_reset=True, seen_result_hashes_added=[f"draft-{number}"],
    )


@pytest.fixture
def near_capacity_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", _ROLLOVER_LIMIT)
    path = tmp_path / "rolling-snapshot.json"
    SessionStore().checkpoint_snapshot(str(path), [_SYSTEM_MESSAGE], meta={}, sequence=1)
    _append_draft(str(path), 2, 1)
    _append_draft(str(path), 3, 2)
    return path, path.with_suffix(".json.journal")


def test_automatic_rollover_preserves_failure_before_base_publish(near_capacity_journal, monkeypatch):
    path, journal = near_capacity_journal
    real_replace = os.replace

    def fail_base_rename(src, dst):
        if Path(dst) == path:
            raise OSError(errno.EIO, "Could not replace base")
        return real_replace(src, dst)

    before = path.read_bytes(), journal.read_bytes()
    with monkeypatch.context() as fail:
        fail.setattr(os, "replace", fail_base_rename)
        with pytest.raises(OSError, match="Could not replace base"):
            _append_draft(str(path), 4, 3)

    assert (path.read_bytes(), journal.read_bytes()) == before
    _append_draft(str(path), 4, 3)
    assert OpenCollab.read_session_snapshot(path)["messages"] == [_SYSTEM_MESSAGE, _draft(3)]


def _interrupt_before_journal_reset(path: Path, journal: Path) -> None:
    storage_module.MAX_SESSION_SNAPSHOT_BYTES = _ROLLOVER_LIMIT
    real_replace = os.replace

    def interrupt_journal_rename(src, dst):
        if Path(dst) == journal:
            os._exit(73)
        return real_replace(src, dst)

    with patch.object(os, "replace", interrupt_journal_rename):
        _append_draft(str(path), 4, 3)


@pytest.mark.parametrize("failure", ["rename-eio", "process-exit"])
def test_automatic_rollover_with_covered_journal_can_be_retried(
    near_capacity_journal, monkeypatch, caplog, failure,
):
    path, journal = near_capacity_journal
    prior_journal = journal.read_bytes()
    if failure == "process-exit":
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_interrupt_before_journal_reset, args=(path, journal))
        worker.start()
        try:
            worker.join(timeout=10)
            assert worker.exitcode == 73
        finally:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
    else:
        real_replace = os.replace

        def fail_journal_rename(src, dst):
            if Path(dst) == journal:
                raise OSError(errno.EIO, "Could not reset journal")
            return real_replace(src, dst)

        with monkeypatch.context() as fail:
            fail.setattr(os, "replace", fail_journal_rename)
            _append_draft(str(path), 4, 3)
        assert "Could not reset journal" in caplog.text

    base = json.loads(path.read_bytes())
    assert base["_autosave_sequence"] == 4
    assert journal.read_bytes() == prior_journal
    assert OpenCollab.read_session_snapshot(path)["messages"] == [_SYSTEM_MESSAGE, _draft(3)]

    with pytest.raises(SnapshotSequenceConflict, match="conflicting autosave sequence 4"):
        _append_draft(str(path), 4, 9)
    with pytest.raises(SnapshotSequenceConflict, match="stale autosave sequence 3"):
        _append_draft(str(path), 3, 2)
    assert journal.read_bytes() == prior_journal

    _append_draft(str(path), 4, 3)
    assert journal.read_bytes() == b""
    _append_draft(str(path), 5, 4)
    restored = OpenCollab.read_session_snapshot(path)
    assert restored["_autosave_sequence"] == 5
    assert restored["messages"] == [_SYSTEM_MESSAGE, _draft(4)]
    assert restored["session_state"] == {"draft": 4, "seen_result_hashes": ["draft-4"]}
