"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading

import opencollab.adapters.storage as storage_mod
import pytest
from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import SAVE_TRIGGERS, AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session as Session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

# Reuse the fakes from the characterization test file (same tests/ directory,
# added to sys.path by pytest's rootdir discovery).
from test_session_characterization import FakeAgent, FakeLLMClient

# Reuse the fakes from the characterization test file (same tests/ directory,
# added to sys.path by pytest's rootdir discovery).


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
        asyncio.run(sub.emit(SessionEvent(type="step_end")))

    assert sub.last_error is error
    assert sub.failure_count == 1
    assert "auto-save failed: disk full" in caplog.text


@pytest.mark.parametrize("fatal", [KeyboardInterrupt("stop"), SystemExit("exit")])
def test_autosave_wraps_worker_base_exceptions_without_escaping(fatal):
    def boom():
        raise fatal

    sub = AutoSaveSubscriber(boom)

    asyncio.run(sub.emit(SessionEvent(type="step_end")))

    assert isinstance(sub.last_error, RuntimeError)
    assert type(fatal).__name__ in str(sub.last_error)
    assert sub.last_error.__cause__ is fatal
    assert sub.failure_count == 1


def test_session_surfaces_autosave_persistence_error():
    error = OSError("snapshot write failed")

    class FailingStore:
        def save(self, path, messages, *, meta=None):
            raise error

        def load_messages(self, path, system_prompt):
            raise AssertionError("load is not used")

        def save_manifest(self, path, manifest):
            raise AssertionError("manifest save is not used")

    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path="unused.json",
        store=FailingStore(),
    )
    asyncio.run(session.add_user_message("hello"))

    assert session.persistence_errors == (error,)


def test_autosave_real_slow_write_does_not_block_event_loop_timeout(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "slow.json")
    store = SessionStore()
    started = threading.Event()
    release = threading.Event()
    real_fsync = storage_mod.os.fsync

    def slow_fsync(fd):
        started.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("test did not release slow fsync")
        return real_fsync(fd)

    monkeypatch.setattr(storage_mod.os, "fsync", slow_fsync)
    sub = AutoSaveSubscriber(
        lambda: store.save(path, [{"role": "user", "content": "slow"}])
    )

    async def scenario():
        emit_task = asyncio.create_task(sub.emit(SessionEvent(type="step_end")))
        assert await asyncio.to_thread(started.wait, 1.0)
        loop = asyncio.get_running_loop()
        before = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.03)
        assert loop.time() - before < 0.5
        assert not emit_task.done()
        release.set()
        await emit_task

    asyncio.run(scenario())
    assert SessionStore().load_messages(path, "fallback") == [
        {"role": "user", "content": "slow"}
    ]


def test_autosave_cancelled_waiter_keeps_write_owned_and_future_saves_ordered():
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    call_count = 0

    def save():
        nonlocal call_count
        call_count += 1
        current = call_count
        calls.append(f"start-{current}")
        if current == 1:
            started.set()
            assert release.wait(timeout=2.0)
        calls.append(f"end-{current}")

    sub = AutoSaveSubscriber(save)

    async def scenario():
        first = asyncio.create_task(sub.emit(SessionEvent(type="step_end")))
        assert await asyncio.to_thread(started.wait, 1.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(sub.pending_tasks) == 1

        second = asyncio.create_task(sub.emit(SessionEvent(type="step_end")))
        await asyncio.sleep(0.03)
        assert calls == ["start-1"]
        assert len(sub.pending_tasks) == 2
        release.set()
        await second

    asyncio.run(scenario())
    assert calls == ["start-1", "end-1", "start-2", "end-2"]
    assert sub.pending_tasks == ()


def test_cancelled_owner_deadline_keeps_late_old_write_before_new_snapshot(
    tmp_path,
):
    path = tmp_path / "ordered.txt"
    old_started = threading.Event()
    release_old = threading.Event()
    current = "old"

    def prepare():
        frozen = current

        def save():
            if frozen == "old":
                old_started.set()
                assert release_old.wait(timeout=2.0)
            path.write_text(frozen, encoding="utf-8")

        return save

    sub = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        nonlocal current
        old_owner = sub.enqueue()
        assert old_owner is not None
        while not old_started.is_set():
            await asyncio.sleep(0)
        old_owner.cancel("cleanup deadline")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(old_owner, timeout=0.6)
        assert isinstance(sub.last_error, TimeoutError)

        current = "new"
        new_owner = sub.enqueue()
        assert new_owner is not None
        await asyncio.sleep(0.03)
        assert new_owner.done() is False
        release_old.set()
        await asyncio.wait_for(new_owner, timeout=0.6)

    asyncio.run(scenario())
    assert path.read_text(encoding="utf-8") == "new"


def test_cross_loop_enqueue_preserves_late_old_write_order(tmp_path):
    path = tmp_path / "cross-loop.txt"
    old_started = threading.Event()
    release_old = threading.Event()
    current = "old"

    def prepare():
        frozen = current

        def save():
            if frozen == "old":
                old_started.set()
                assert release_old.wait(timeout=2.0)
            path.write_text(frozen, encoding="utf-8")

        return save

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def abandon_old_loop():
        owner = subscriber.enqueue()
        assert owner is not None
        while not old_started.is_set():
            await asyncio.sleep(0)
        owner.cancel("close loop A")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=0.6)

    asyncio.run(abandon_old_loop())
    current = "new"

    async def commit_in_new_loop():
        owner = subscriber.enqueue()
        assert owner is not None
        await asyncio.sleep(0.03)
        assert owner.done() is False
        release_old.set()
        await asyncio.wait_for(owner, timeout=0.6)

    asyncio.run(commit_in_new_loop())
    assert path.read_text(encoding="utf-8") == "new"


def test_permanently_blocked_save_does_not_hold_asyncio_run_process_open():
    package_root = os.path.dirname(os.path.dirname(__file__))
    script = r'''
import asyncio
import threading

from opencollab.application.autosave import AutoSaveSubscriber


started = threading.Event()
blocked = threading.Event()


def save_forever():
    started.set()
    blocked.wait()


async def main():
    subscriber = AutoSaveSubscriber(save_forever)
    owner = subscriber.enqueue()
    while not started.is_set():
        await asyncio.sleep(0)
    owner.cancel("shutdown")
    try:
        await owner
    except asyncio.CancelledError:
        pass
    assert isinstance(subscriber.last_error, TimeoutError)


asyncio.run(main())
'''
    process_env = dict(os.environ)
    process_env["PYTHONPATH"] = package_root
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        env=process_env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_session_autosave_freezes_nested_payload_before_background_io():
    started = threading.Event()
    release = threading.Event()
    captured: list[list[dict]] = []

    class BlockingStore:
        def save(self, path, messages, *, meta=None):
            started.set()
            assert release.wait(timeout=2.0)
            captured.append(messages)

        def load_messages(self, path, system_prompt):
            raise AssertionError("load is not used")

        def save_manifest(self, path, manifest):
            raise AssertionError("manifest save is not used")

    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path="unused.json",
        store=BlockingStore(),
    )
    nested_call = {"id": "original", "function": {"name": "bash"}}
    session.state.append_message(
        {"role": "assistant", "content": "", "tool_calls": [nested_call]}
    )

    async def scenario():
        emit_task = asyncio.create_task(
            session.event_bus.emit(SessionEvent(type="step_end"))
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        nested_call["id"] = "mutated-after-submit"
        release.set()
        await emit_task

    asyncio.run(scenario())
    assert captured[0][-1]["tool_calls"][0]["id"] == "original"


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


def test_session_with_auto_save_path_writes_on_user_message(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path=str(path),
    )
    asyncio.run(session.add_user_message("hello"))
    assert path.exists()
    contents = path.read_text()
    assert "hello" in contents


def test_session_without_auto_save_path_does_not_subscribe():
    session = Session(agent=FakeAgent(), llm=FakeLLMClient())
    assert not any(
        isinstance(t, AutoSaveSubscriber) for t in session.event_bus._targets
    )


def test_session_external_sink_and_autosave_coexist(tmp_path):
    path = tmp_path / "auto.jsonl"
    received: list[str] = []

    class CapturingSink:
        async def emit(self, event):
            received.append(event.type)

    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient(),
        auto_save_path=str(path),
        event_sink=CapturingSink(),
    )
    asyncio.run(session.add_user_message("hi"))
    assert path.exists()
    assert "user_message_appended" in received
