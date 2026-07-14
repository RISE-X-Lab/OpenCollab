"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio
import logging

import pytest
from opencollab.application.autosave import SAVE_TRIGGERS, AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session as Session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

# Reuse the fakes from the characterization test file (same tests/ directory,
# added to sys.path by pytest's rootdir discovery).
from test_session_characterization import FakeAgent, FakeLLMClient


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


def test_autosave_queues_frozen_operations_in_order():
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
    assert saved == ["first", "second"]
    assert subscriber.pending_tasks == ()


def test_cancelling_emit_keeps_submitted_save_owned():
    prepared = asyncio.Event()
    saved: list[str] = []

    def prepare():
        prepared.set()
        return lambda: saved.append("saved")

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        emit = asyncio.create_task(subscriber.emit(SessionEvent(type="step_end")))
        await prepared.wait()
        emit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await emit
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
