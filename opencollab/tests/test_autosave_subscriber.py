"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.bootstrap import build_session as Session
from opencollab.application.autosave import AutoSaveSubscriber, SAVE_TRIGGERS
from opencollab.application.event_bus import EventBus
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

# Reuse the fakes from the characterization test file (same tests/ directory,
# added to sys.path by pytest's rootdir discovery).
from test_session_characterization import FakeAgent, FakeLLMClient, llm_response


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


def test_autosave_subscriber_swallows_save_errors():
    def boom():
        raise OSError("disk full")

    sub = AutoSaveSubscriber(boom)
    # Must not raise.
    asyncio.run(sub.emit(SessionEvent(type="step_end")))


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


def test_direct_compaction_apply_autosaves_compacted_messages(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = Session(
        agent=FakeAgent(),
        llm=FakeLLMClient([llm_response(content="summary")]),
        auto_save_path=str(path),
    )
    session.messages.extend(
        {"role": "user", "content": f"message {idx}"} for idx in range(10)
    )

    asyncio.run(session.compactor.compact(apply=True))

    assert "Context compacted" in path.read_text()
