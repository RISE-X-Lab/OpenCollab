"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

# Reuse the fakes from the characterization test file (same tests/ directory,
# added to sys.path by pytest's rootdir discovery).
from test_session_characterization import FakeAgent, FakeLLMClient, llm_response

from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import SAVE_TRIGGERS, AutoSaveSubscriber
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent


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


def test_autosave_coalesces_pending_generations_without_blocking_emit():
    current = {"value": ""}
    saved: list[str] = []

    def prepare():
        frozen = current["value"]

        def save():
            time.sleep(0.02)
            saved.append(frozen)

        return save

    subscriber = AutoSaveSubscriber(lambda: None, prepare_fn=prepare)

    async def scenario():
        started = time.monotonic()
        for index in range(10):
            current["value"] = str(index)
            await subscriber.emit(SessionEvent(type="step_end"))
        emit_elapsed = time.monotonic() - started
        await asyncio.gather(*subscriber.pending_tasks)
        return emit_elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 0.05
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


@pytest.mark.parametrize("trigger", sorted(SAVE_TRIGGERS))
def test_autosave_journal_recovers_every_trigger_with_linear_write_growth(
    tmp_path,
    trigger,
):
    def persist_steps(name: str, count: int):
        path = tmp_path / name
        store = _CountingSessionStore()
        session = Session(
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
            str(path),
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


@pytest.mark.parametrize(
    ("mode", "expected_phase"),
    [("done", "done"), ("stopped", "stopped"), ("error", "error")],
)
def test_terminal_phase_is_committed_by_final_autosave(
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
        agent=FakeAgent(),
        llm=TerminalLLM(),
        auto_save_path=str(path),
        max_budget_tokens=100,
    )
    asyncio.run(session.add_user_message("go"))
    if mode == "stopped":
        session.state.set_used_tokens(100)

    if mode == "error":
        with pytest.raises(RuntimeError, match="provider failed"):
            asyncio.run(session.run_loop())
    else:
        asyncio.run(session.run_loop())

    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["session_state"]["phase"] == expected_phase


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
