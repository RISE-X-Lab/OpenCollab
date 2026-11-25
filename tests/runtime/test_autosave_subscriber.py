"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from opencollab.adapters import safe_files
from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import SAVE_TRIGGERS, AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import SnapshotCapacityError, SnapshotSequenceConflict
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.session_characterization_test_support import FakeAgent, FakeLLMClient, llm_response


@pytest.mark.parametrize("trigger", sorted(SAVE_TRIGGERS))
def test_autosave_subscriber_fires_on_each_trigger(trigger):
    calls = []
    sub = AutoSaveSubscriber(lambda: calls.append("saved"))
    asyncio.run(sub.emit(SessionEvent(type=trigger)))
    assert calls == ["saved"]

def test_autosave_subscriber_ignores_other_events():
    calls = []
    sub = AutoSaveSubscriber(lambda: calls.append("saved"))
    asyncio.run(sub.emit(SessionEvent(type="text_delta", data={"content": "x"})))
    asyncio.run(sub.emit(SessionEvent(type="tool_start", data={"tool": "bash"})))
    assert calls == []

def test_autosave_subscriber_records_save_errors(caplog):
    error = OSError("disk full")

    def boom():
        raise error

    sub = AutoSaveSubscriber(boom)
    with caplog.at_level(logging.WARNING):
        async def scenario():
            await sub.emit(SessionEvent(type="step_end"))
            await asyncio.gather(*sub.pending_tasks, return_exceptions=True)

        asyncio.run(scenario())

    assert sub.last_error is error
    assert sub.failure_count == 1
    assert "auto-save failed: disk full" in caplog.text

@pytest.mark.parametrize("fatal", [KeyboardInterrupt("stop"), SystemExit("exit")])
def test_autosave_wraps_worker_base_exceptions_without_escaping(fatal):
    def boom():
        raise fatal

    sub = AutoSaveSubscriber(boom)

    async def scenario():
        await sub.emit(SessionEvent(type="step_end"))
        await asyncio.gather(*sub.pending_tasks, return_exceptions=True)

    asyncio.run(scenario())

    assert isinstance(sub.last_error, RuntimeError)
    assert type(fatal).__name__ in str(sub.last_error)
    assert sub.last_error.__cause__ is fatal
    assert sub.failure_count == 1

def test_session_surfaces_autosave_persistence_error(local_environment_factory):
    error = OSError("snapshot write failed")

    class FailingStore:
        def save(self, path, messages, *, meta=None):
            raise error

        def load_messages(self, path, system_prompt):
            raise AssertionError("load is not used")

        def save_manifest(self, path, manifest):
            raise AssertionError("manifest save is not used")

    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path="unused.json",
        store=FailingStore(),
    )
    asyncio.run(session.add_user_message("hello"))

    assert session.persistence_errors == (error,)

def test_autosave_coalesces_generations_not_yet_started():
    current = {"value": "first"}
    saved: list[str] = []

    def prepare():
        frozen = current["value"]
        return lambda: saved.append(frozen)

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        first = subscriber.enqueue()
        current["value"] = "second"
        second = subscriber.enqueue()
        assert first is not None and second is not None
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert saved == ["second"]
    assert subscriber.pending_tasks == ()

# A timer releases the sink independently so waiting in emit would block until that release.
SINK_HELD_SECONDS = 2.0

# The deadline separates a prompt hand-off from waiting for the held sink.
NON_BLOCKING_EMIT_SECONDS = 0.5

def test_autosave_coalesces_pending_generations_without_blocking_emit():
    current = {"value": ""}
    saved: list[str] = []
    release = threading.Event()

    def prepare():
        frozen = current["value"]

        def save():
            release.wait(10.0)
            saved.append(frozen)

        return save

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        releaser = threading.Timer(SINK_HELD_SECONDS, release.set)
        releaser.start()
        try:
            started_at = time.monotonic()
            for index in range(10):
                current["value"] = str(index)
                await subscriber.emit(SessionEvent(type="step_end"))
                assert saved == []
            assert time.monotonic() - started_at < NON_BLOCKING_EMIT_SECONDS
        finally:
            releaser.cancel()
            release.set()
        await asyncio.gather(*subscriber.pending_tasks)

    asyncio.run(scenario())

    assert saved == ["0", "9"]

class _CountingSessionStore(SessionStore):
    def __init__(self):
        self.persisted_bytes = 0

    def _atomic_json_write(self, path, value):
        self.persisted_bytes += len(
            json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        )
        return super()._atomic_json_write(path, value)

    def _append_journal_record(self, path, record):
        self.persisted_bytes += len(
            (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        return super()._append_journal_record(path, record)

class _SwitchableCheckpointStore(SessionStore):
    def __init__(self, error: Exception):
        self.error = error
        self.fail_checkpoint = False

    def checkpoint_snapshot(self, *args, **kwargs):
        if self.fail_checkpoint:
            raise self.error
        return super().checkpoint_snapshot(*args, **kwargs)

class _CompletingLLM:
    async def complete(self, **_kwargs):
        return llm_response(content="done")

@pytest.mark.parametrize("trigger", sorted(SAVE_TRIGGERS))
def test_autosave_journal_recovers_every_trigger_with_linear_write_growth(
    local_environment_factory,
    tmp_path,
    trigger,
):
    def persist_steps(name: str, count: int):
        path = tmp_path / name
        store = _CountingSessionStore()
        session = Session(
            env=local_environment_factory(),
            agent=FakeAgent(),
            llm=FakeLLMClient(),
            auto_save_path=str(path),
            store=store,
        )

        async def scenario():
            for step in range(1, count + 1):
                session.state.append_message(
                    {
                        "role": (
                            "assistant"
                            if trigger == "step_end"
                            else "user"
                        ),
                        "content": f"durable {trigger} {step}: " + ("x" * 128),
                    }
                )
                session.state.turn.seen_result_hashes.add(f"result-hash-{step:04d}")
                session.step_count = step
                await session.event_bus.emit(SessionEvent(type=trigger))
                pending = session.pending_cleanup_tasks
                if pending:
                    await asyncio.gather(*pending)

        asyncio.run(scenario())
        restored = load_session(
            str(path), env=local_environment_factory(),
            agent=FakeAgent(),
            llm=FakeLLMClient(),
        )
        return store.persisted_bytes, restored

    bytes_500, restored_500 = persist_steps(f"{trigger}-500.json", 500)
    bytes_1000, restored_1000 = persist_steps(f"{trigger}-1000.json", 1000)

    assert restored_500.step_count == 500
    assert restored_500.messages[-1]["content"].startswith(
        f"durable {trigger} 500:"
    )
    assert len(restored_500.state.turn.seen_result_hashes) == 500
    assert restored_1000.step_count == 1000
    assert restored_1000.messages[-1]["content"].startswith(
        f"durable {trigger} 1000:"
    )
    assert len(restored_1000.state.turn.seen_result_hashes) == 1000
    assert bytes_1000 <= bytes_500 * 2.5

def test_cancelling_owned_worker_finishes_submitted_save():
    prepared = asyncio.Event()
    saved: list[str] = []

    def prepare():
        prepared.set()

        def save():
            time.sleep(0.02)
            saved.append("saved")

        return save

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        owner = subscriber.enqueue()
        assert owner is not None
        await prepared.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        pending = subscriber.pending_tasks
        if pending:
            await pending[0]

    asyncio.run(scenario())
    assert saved == ["saved"]

def test_event_bus_fans_out_to_multiple_subscribers():
    bus = EventBus()
    a, b = [], []
    bus.subscribe(lambda evt: a.append(evt.type))
    bus.subscribe(lambda evt: b.append(evt.type))
    asyncio.run(bus.emit(SessionEvent(type="ping")))
    assert a == ["ping"] and b == ["ping"]

def test_event_bus_failure_in_one_subscriber_does_not_silence_others():
    bus = EventBus()
    seen = []

    def bad(evt):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda evt: seen.append(evt.type))
    asyncio.run(bus.emit(SessionEvent(type="ping")))
    assert seen == ["ping"]

def test_event_bus_subscriber_self_cancel_does_not_silence_others():
    bus = EventBus()
    seen = []

    async def self_cancel(_event):
        raise asyncio.CancelledError

    bus.subscribe(self_cancel)
    bus.subscribe(lambda event: seen.append(event.type))

    asyncio.run(bus.emit(SessionEvent(type="ping")))

    assert seen == ["ping"]

def test_event_bus_external_cancellation_still_propagates():
    bus = EventBus()
    started = asyncio.Event()
    seen = []

    async def blocking(_event):
        started.set()
        await asyncio.Event().wait()

    bus.subscribe(blocking)
    bus.subscribe(lambda event: seen.append(event.type))

    async def scenario():
        emit = asyncio.create_task(bus.emit(SessionEvent(type="ping")))
        await started.wait()
        emit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await emit

    asyncio.run(scenario())

    assert seen == []

def test_session_with_auto_save_path_writes_on_user_message(local_environment_factory, tmp_path):
    path = tmp_path / "auto.jsonl"
    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path=str(path),
    )
    asyncio.run(session.add_user_message("hello"))
    assert path.exists()
    contents = path.read_text()
    assert "hello" in contents

def test_autosave_retries_journal_record_after_fsync_error(local_environment_factory, tmp_path, monkeypatch):
    path = tmp_path / "fsync-retry.json"
    store = SessionStore()
    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path=str(path),
        store=store,
    )
    real_fsync = __import__("os").fsync
    journal_path = tmp_path / "fsync-retry.json.journal"
    journal_path.touch()
    journal_identity = (journal_path.stat().st_dev, journal_path.stat().st_ino)
    fail_once = True
    injected_error = OSError(
        "fsync reported failure after journal bytes were written"
    )

    def fsync_after_write(fd):
        nonlocal fail_once
        info = __import__("os").fstat(fd)
        if fail_once and (info.st_dev, info.st_ino) == journal_identity:
            fail_once = False
            real_fsync(fd)
            raise injected_error
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", fsync_after_write)

    async def persist_current_state():
        await asyncio.gather(*session.pending_cleanup_tasks)

    async def scenario():
        session._next_auto_save_checkpoint = 1
        await session.add_user_message("first message")
        await persist_current_state()
        # The failed write leaves its reservation uncommitted and its bytes
        # rolled back; there is no durable snapshot or journal record yet.
        assert session._auto_save_sequence == 0
        assert store._read_journal_records(str(path)) == []
        with pytest.raises(FileNotFoundError):
            store.load_snapshot(str(path), session.agent.system_prompt)

        session.state.append_message(
            {"role": "user", "content": "second message"}
        )
        await session.event_bus.emit(SessionEvent(type="user_message_appended"))
        await persist_current_state()

    asyncio.run(scenario())

    before_restore = store.load_snapshot(str(path), "fallback")
    # Reservation 1 belongs to the failed attempt. The changed-transcript
    # retry reserves and durably checkpoints sequence 2 without a duplicate 1.
    assert session._auto_save_sequence == 2
    assert before_restore["_autosave_sequence"] == 2
    assert [message["content"] for message in before_restore["messages"][-2:]] == [
        "first message",
        "second message",
    ]

    restored = load_session(
        str(path), env=local_environment_factory(),
        agent=FakeAgent(),
        llm=FakeLLMClient(),
    )
    assert restored.messages[-2:]==[
        {"role": "user", "content": "first message"},
        {"role": "user", "content": "second message"},
    ]
    assert restored._auto_save_sequence == before_restore["_autosave_sequence"]
    assert session.persistence_errors == (injected_error,)
    assert journal_path.read_bytes() == b""

@pytest.mark.parametrize("error_type", [OSError, SnapshotCapacityError, SnapshotSequenceConflict])
async def test_scheduled_checkpoint_failure_after_durable_append(
    local_environment_factory, tmp_path, caplog, error_type,
):
    path = tmp_path / "scheduled.json"
    error = error_type("scheduled checkpoint failed")
    store = _SwitchableCheckpointStore(error)
    store.fail_checkpoint = True
    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=_CompletingLLM(),
        auto_save_path=str(path),
        store=store,
    )

    await session.add_user_message("go")
    await asyncio.gather(*session.pending_cleanup_tasks)

    assert not path.exists()
    saved = store.load_snapshot(str(path), session.agent.system_prompt)
    assert saved["messages"][-1]["content"] == "go"
    if error_type is SnapshotSequenceConflict:
        assert session.persistence_errors == (error,)
    else:
        assert session.persistence_errors == ()
        assert "session checkpoint deferred after durable autosave: scheduled checkpoint failed" in caplog.text

    store.fail_checkpoint = False
    await session.enqueue_auto_save()
    assert json.loads(path.read_bytes())["_autosave_sequence"] == saved["_autosave_sequence"] + 1
    assert path.with_suffix(".json.journal").read_bytes() == b""
    assert await session.run_loop() == "done"

@pytest.mark.parametrize(
    ("mode", "expected_phase"),
    [("done", "done"), ("stopped", "stopped"), ("error", "error")],
)
def test_terminal_phase_is_committed_by_final_autosave(
    local_environment_factory,
    tmp_path, mode, expected_phase
):
    class TerminalLLM:
        async def complete(self, **_kwargs):
            if isinstance(response, Exception):
                raise response
            return response

    path = tmp_path / f"{mode}.json"
    response = (
        RuntimeError("provider failed")
        if mode == "error"
        else llm_response(content="done")
    )
    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=TerminalLLM(),
        auto_save_path=str(path),
        max_budget_tokens=500,
    )
    asyncio.run(session.add_user_message("go"))
    if mode == "stopped":
        session.state.set_used_tokens(500)

    if mode == "error":
        with pytest.raises(RuntimeError, match="provider failed"):
            asyncio.run(session.run_loop())
    else:
        asyncio.run(session.run_loop())

    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["session_state"]["phase"] == expected_phase

def test_terminal_checkpoint_failure_does_not_mask_provider_error(local_environment_factory, tmp_path):
    provider_error = RuntimeError("provider failed")
    checkpoint_error = OSError("checkpoint failed")

    class FailingLLM:
        async def complete(self, **_kwargs):
            raise provider_error

    async def scenario():
        store = _SwitchableCheckpointStore(checkpoint_error)
        session = Session(
            env=local_environment_factory(),
            agent=FakeAgent(),
            llm=FailingLLM(),
            auto_save_path=str(tmp_path / "provider-error.json"),
            store=store,
        )
        await session.add_user_message("go")
        if session.pending_cleanup_tasks:
            await asyncio.gather(*session.pending_cleanup_tasks)
        store.fail_checkpoint = True

        with pytest.raises(RuntimeError, match="provider failed") as caught:
            await session.run_loop()

        assert caught.value is provider_error
        assert any(
            "terminal checkpoint also failed: OSError: checkpoint failed" in note
            for note in getattr(caught.value, "__notes__", ())
        )
        assert session.persistence_errors == (checkpoint_error,)

    asyncio.run(scenario())

def test_terminal_checkpoint_failure_does_not_mask_cancellation(local_environment_factory, tmp_path):
    cancellation = asyncio.CancelledError("caller cancelled")
    checkpoint_error = OSError("checkpoint failed")

    async def scenario():
        store = _SwitchableCheckpointStore(checkpoint_error)
        session = Session(
            env=local_environment_factory(),
            agent=FakeAgent(),
            llm=FakeLLMClient(),
            auto_save_path=str(tmp_path / "cancelled.json"),
            store=store,
        )
        await session.add_user_message("go")
        if session.pending_cleanup_tasks:
            await asyncio.gather(*session.pending_cleanup_tasks)
        store.fail_checkpoint = True

        async def cancelled_runner(_cancel_event=None):
            session.state.cancel("caller cancelled")
            raise cancellation

        session.runner.run_loop = cancelled_runner
        with pytest.raises(asyncio.CancelledError) as caught:
            await session.run_loop()

        assert caught.value is cancellation
        assert any(
            "terminal checkpoint also failed: OSError: checkpoint failed" in note
            for note in getattr(caught.value, "__notes__", ())
        )
        assert session.persistence_errors == (checkpoint_error,)

    asyncio.run(scenario())

def test_terminal_checkpoint_failure_surfaces_without_primary_error(local_environment_factory, tmp_path):
    checkpoint_error = OSError("checkpoint failed")

    async def scenario():
        store = _SwitchableCheckpointStore(checkpoint_error)
        session = Session(
            env=local_environment_factory(),
            agent=FakeAgent(),
            llm=_CompletingLLM(),
            auto_save_path=str(tmp_path / "completed.json"),
            store=store,
        )
        await session.add_user_message("go")
        if session.pending_cleanup_tasks:
            await asyncio.gather(*session.pending_cleanup_tasks)
        store.fail_checkpoint = True

        with pytest.raises(OSError, match="checkpoint failed") as caught:
            await session.run_loop()

        assert caught.value is checkpoint_error
        assert session.persistence_errors == (checkpoint_error,)

    asyncio.run(scenario())

@pytest.mark.parametrize("failure_stage", ["base_rename", "directory_sync", "journal_reset", "append_then_reset"])
async def test_terminal_result_requires_a_durable_base_but_can_leave_covered_journal(
    local_environment_factory, tmp_path, monkeypatch, caplog, failure_stage,
):
    path = tmp_path / "completed.json"
    journal = path.with_suffix(".json.journal")
    session = Session(
        env=local_environment_factory(), agent=FakeAgent(), llm=_CompletingLLM(),
        auto_save_path=str(path),
    )
    error = OSError(errno.EIO, f"checkpoint {failure_stage}")
    append_error = OSError(errno.EIO, "terminal append")
    append = session.store.append_snapshot_delta
    save = session.save
    replace_file = os.replace

    def fail_rename(source, destination):
        failing_path = path if failure_stage == "base_rename" else journal
        if Path(destination) == failing_path:
            raise error
        return replace_file(source, destination)

    def save_with_failure(target):
        if failure_stage == "directory_sync":
            with patch.object(safe_files, "_fsync_directory", side_effect=error):
                save(target)
        else:
            with patch.object(os, "replace", fail_rename):
                save(target)

    def fail_terminal_append(*args, **kwargs):
        if kwargs["meta"]["session_state"]["phase"] == "done":
            raise append_error
        return append(*args, **kwargs)

    try:
        await session.add_user_message("initial turn")
        assert await session.run_loop() == "done"
        if failure_stage == "append_then_reset":
            monkeypatch.setattr(session.store, "append_snapshot_delta", fail_terminal_append)
        monkeypatch.setattr(session, "save", save_with_failure)
        await session.add_user_message("go")
        if failure_stage == "journal_reset":
            assert await session.run_loop() == "done"
            base = json.loads(path.read_bytes())
            assert base["session_state"]["phase"] == "done"
            assert base["messages"][-1]["content"] == "done"
            assert journal.stat().st_size > 0
            assert session.persistence_errors == ()
            assert "could not compact covered session journal" in caplog.text

            with pytest.raises(OSError) as manual:
                session.save(str(path))
            assert manual.value is error
        else:
            with pytest.raises(OSError) as terminal:
                await session.run_loop()
            assert terminal.value is error
            expected = (append_error, error) if failure_stage == "append_then_reset" else (error,)
            assert session.persistence_errors == expected
    finally:
        await session.aclose()
        await session.env.cleanup()

def test_cancelling_terminal_checkpoint_waits_for_owned_write(local_environment_factory, tmp_path):
    class BlockingCheckpointStore(SessionStore):
        def __init__(self):
            self.block_checkpoint = False
            self.checkpoint_started = threading.Event()
            self.release_checkpoint = threading.Event()

        def checkpoint_snapshot(self, *args, **kwargs):
            if self.block_checkpoint:
                self.checkpoint_started.set()
                if not self.release_checkpoint.wait(timeout=2):
                    raise TimeoutError("test checkpoint was not released")
            return super().checkpoint_snapshot(*args, **kwargs)

    async def scenario():
        store = BlockingCheckpointStore()
        path = tmp_path / "cancel-during-checkpoint.json"
        session = Session(
            env=local_environment_factory(),
            agent=FakeAgent(),
            llm=_CompletingLLM(),
            auto_save_path=str(path),
            store=store,
        )
        await session.add_user_message("go")
        if session.pending_cleanup_tasks:
            await asyncio.gather(*session.pending_cleanup_tasks)
        session._next_auto_save_checkpoint = 1_000
        store.block_checkpoint = True

        run_task = asyncio.create_task(session.run_loop())
        assert await asyncio.to_thread(store.checkpoint_started.wait, 1)
        run_task.cancel()
        await asyncio.sleep(0)
        assert not run_task.done()
        store.release_checkpoint.set()

        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert not session.pending_cleanup_tasks
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert snapshot["session_state"]["phase"] == "done"

    asyncio.run(scenario())

def test_session_external_sink_and_autosave_coexist(local_environment_factory, tmp_path):
    path = tmp_path / "auto.jsonl"
    received: list[str] = []

    class CapturingSink:
        async def emit(self, event):
            received.append(event.type)

    session = Session(
        env=local_environment_factory(),
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path=str(path),
        event_sink=CapturingSink(),
    )
    asyncio.run(session.add_user_message("hi"))
    assert path.exists()
    assert "user_message_appended" in received
