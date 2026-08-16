"""Characterization tests for Session construction.

Lock today's observable behavior of ``Session.__init__``, ``snapshot``,
and ``load`` before Step13 moves runtime construction into
``bootstrap/container.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.scheduler_types import LaunchSpec
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session, snapshot_session


def run(coro):
    return asyncio.run(coro)


def test_event_bus_satisfies_event_publisher_port():
    from opencollab.application.event_bus import EventBus
    from opencollab.application.ports import EventPublisherPort

    bus: EventPublisherPort = EventBus()
    assert hasattr(bus, "emit")
    assert callable(getattr(bus, "emit"))


class _FakeAgent:
    def __init__(self):
        self.name = "fake-agent"
        self.system_prompt = "You are a fake agent."
        self.tools = []
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.api_key = "fake-key"
        self.base_url = None
        self.temperature = 0.0

    def tool_schemas(self):
        return []

    def find_tool(self, name):
        return None


class _FakeLLM:
    async def complete(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("FakeLLM.complete should not be called")


class _ForkableEnvironment:
    workspace = "."
    host_workspace = "."
    source_workspace = "."
    local_filesystem = False

    def fork_snapshot(self):
        return _ForkableEnvironment()


def _new_session(**overrides) -> Session:
    kwargs = dict(agent=_FakeAgent(), llm=_FakeLLM())
    kwargs.update(overrides)
    return Session(**kwargs)


def test_session_builds_runtime_collaborators_eagerly():
    session = _new_session()

    assert isinstance(session.tool_execution, ToolExecutionUseCase)
    assert isinstance(session.runner, SessionRunUseCase)
    assert isinstance(session.store, SessionStore)


def test_session_pending_cleanup_includes_tool_execution_owners():
    async def scenario():
        session = _new_session()
        owner = asyncio.create_task(asyncio.Event().wait())
        session.tool_execution._pending_cleanup_tasks.add(owner)
        try:
            assert owner in session.pending_cleanup_tasks
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    run(scenario())


def test_session_seed_user_messages_follow_system_prompt():
    session = _new_session(
        seed_user_messages=[{"role": "user", "content": "Task:\nbuild it"}]
    )
    assert session.messages == [
        {"role": "system", "content": "You are a fake agent."},
        {"role": "user", "content": "Task:\nbuild it"},
    ]


def test_session_wires_a_default_shaper_pipeline():
    from opencollab.application.shaping import ShaperPipeline

    session = _new_session()
    assert isinstance(session.runner.shaper, ShaperPipeline)


def test_session_event_bus_reaches_injected_sink():
    from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

    seen: list = []

    async def sink(event):
        seen.append(event)

    session = _new_session(event_sink=sink)
    event = SessionEvent(type="marker", data={"k": 1})
    run(session.event_bus.emit(event))

    assert seen == [event]


def test_session_with_auto_save_path_subscribes_autosave_subscriber(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = _new_session(auto_save_path=str(path))

    subs = [t for t in session.event_bus._targets if isinstance(t, AutoSaveSubscriber)]
    assert len(subs) == 1


def test_session_without_auto_save_path_does_not_subscribe_autosave():
    session = _new_session()
    subs = [t for t in session.event_bus._targets if isinstance(t, AutoSaveSubscriber)]
    assert subs == []


def test_session_user_message_appended_triggers_autosave(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = _new_session(auto_save_path=str(path))

    run(session.add_user_message("hello"))

    assert os.path.exists(path)
    with open(path) as f:
        saved = json.load(f)
    user_msgs = [m for m in saved["messages"] if m["role"] == "user"]
    assert any(m["content"] == "hello" and "timestamp" in m for m in user_msgs)


def test_apply_launch_recovers_journal_when_atomic_base_is_absent(tmp_path):
    path = tmp_path / "journal-only.json"
    store = SessionStore()
    store.append_snapshot_delta(
        str(path),
        sequence=1,
        replace_from=0,
        messages=[
            {"role": "system", "content": "You are a fake agent."},
            {"role": "assistant", "content": "durable step"},
        ],
        meta={
            "snapshot_version": 1,
            "session_state": {
                "step_count": 7,
                "phase": "idle",
            },
        },
    )
    assert not path.exists()

    session = _new_session(auto_save_path=str(path), store=store)
    session.apply_launch(
        LaunchSpec(
            session_file=str(path),
            auto_save_path=str(path),
        )
    )

    assert session.step_count == 7
    assert session.messages[-1]["content"] == "durable step"


def test_checkpoint_compaction_does_not_erase_concurrent_journal_delta(tmp_path):
    path = tmp_path / "race.json"
    store = SessionStore()
    store.append_snapshot_delta(
        str(path),
        sequence=2,
        replace_from=0,
        messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "old"},
        ],
        meta={"snapshot_version": 1, "session_state": {}},
    )
    base_written = threading.Event()
    release_compaction = threading.Event()
    original_write = store._atomic_json_write

    def paused_write(target, value):
        original_write(target, value)
        base_written.set()
        assert release_compaction.wait(timeout=2)

    store._atomic_json_write = paused_write
    checkpoint = threading.Thread(
        target=store.checkpoint_snapshot,
        args=(
            str(path),
            [{"role": "system", "content": "system"},
             {"role": "assistant", "content": "base"}],
        ),
        kwargs={"meta": {"snapshot_version": 1, "session_state": {}}, "sequence": 2},
    )
    checkpoint.start()
    assert base_written.wait(timeout=2)
    append_errors = []

    def append_new_delta():
        try:
            store.append_snapshot_delta(
                str(path),
                sequence=3,
                replace_from=2,
                messages=[{"role": "assistant", "content": "new"}],
                meta={"snapshot_version": 1, "session_state": {}},
            )
        except BaseException as exc:  # pragma: no cover - diagnostic assertion below
            append_errors.append(exc)

    append = threading.Thread(target=append_new_delta)
    append.start()
    # The append must wait for compaction's lock rather than racing its clear.
    release_compaction.set()
    append.join(timeout=2)
    release_compaction.set()
    checkpoint.join(timeout=2)
    assert not append.is_alive()
    assert not checkpoint.is_alive()
    assert append_errors == []
    restored = store.load_snapshot(str(path), "system")
    assert restored["_autosave_sequence"] == 3
    assert restored["messages"][-1]["content"] == "new"


def test_checkpoint_rejects_sequence_older_than_persisted_journal(tmp_path):
    path = tmp_path / "stale-checkpoint.json"
    store = SessionStore()
    base = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "base"},
    ]
    meta = {"snapshot_version": 1, "session_state": {}}
    store.checkpoint_snapshot(str(path), base, meta=meta, sequence=1)
    store.append_snapshot_delta(
        str(path),
        sequence=2,
        replace_from=2,
        messages=[{"role": "assistant", "content": "newer-delta"}],
        meta=meta,
    )

    with pytest.raises(ValueError, match="persisted sequence 2"):
        store.checkpoint_snapshot(str(path), base, meta=meta, sequence=1)

    restored = store.load_snapshot(str(path), "system")
    assert restored["_autosave_sequence"] == 2
    assert restored["messages"][-1]["content"] == "newer-delta"


def test_apply_launch_checkpoints_restore_into_distinct_autosave_target(tmp_path):
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    store = SessionStore()
    store.checkpoint_snapshot(
        str(source),
        [
            {"role": "system", "content": "You are a fake agent."},
            {"role": "user", "content": "resume me"},
        ],
        meta={
            "snapshot_version": 1,
            "session_state": {
                "step_count": 2,
                "phase": "idle",
            },
        },
        sequence=2,
    )

    session = _new_session(auto_save_path=str(target), store=store)
    session.apply_launch(
        LaunchSpec(
            session_file=str(source),
            auto_save_path=str(target),
        )
    )

    checkpoint = store.load_snapshot(str(target), session.agent.system_prompt)
    assert checkpoint["_autosave_sequence"] == 2
    assert checkpoint["messages"][-1]["content"] == "resume me"

    async def append_and_flush():
        await session.add_user_message("continue here")
        pending = session.pending_cleanup_tasks
        if pending:
            await asyncio.gather(*pending)

    run(append_and_flush())
    restored = load_session(
        str(target),
        agent=_FakeAgent(),
        llm=_FakeLLM(),
        store=store,
    )
    assert restored._auto_save_sequence == 3
    assert restored.messages[-1]["content"] == "continue here"


def test_relative_save_alias_uses_autosave_checkpoint(tmp_path, monkeypatch):
    target = tmp_path / "alias.json"
    session = _new_session(auto_save_path=str(target))
    session._auto_save_sequence = 4
    session.messages.append({"role": "user", "content": "durable"})
    monkeypatch.chdir(tmp_path)

    session.save("alias.json")

    saved = json.loads(target.read_text())
    assert saved["_autosave_sequence"] == 4
    assert (tmp_path / "alias.json.journal").read_bytes() == b""


def test_session_snapshot_returns_independent_session_without_autosave(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = _new_session(auto_save_path=str(path), env=_ForkableEnvironment())
    session.messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    session.used_tokens = 42
    session.step_count = 3

    snap = snapshot_session(session)

    assert snap is not session
    assert snap.messages == session.messages
    assert snap.used_tokens == 42
    assert snap.step_count == 3
    # Snapshot must not inherit the AutoSaveSubscriber.
    subs = [t for t in snap.event_bus._targets if isinstance(t, AutoSaveSubscriber)]
    assert subs == []
    # Mutating snap.messages must not mutate the original.
    snap.messages.append({"role": "user", "content": "isolated"})
    assert {"role": "user", "content": "isolated"} not in session.messages


def test_session_snapshot_preserves_queued_external_turn():
    session = _new_session(env=_ForkableEnvironment())
    run(session.add_user_message("resume this request"))

    snap = snapshot_session(session)

    assert snap.state.pending_external_user_turn == session.state.pending_external_user_turn
    assert snap.state.pending_external_user_turn is not session.state.pending_external_user_turn


def test_session_snapshot_preserves_external_sink():
    seen: list = []

    async def sink(event):
        seen.append(event)

    session = _new_session(event_sink=sink, env=_ForkableEnvironment())
    snap = snapshot_session(session)

    # External subscribers are never silently shared with a runnable snapshot.
    targets = list(snap.event_bus._targets)
    assert sink not in targets
    observed = snapshot_session(session, event_sink=sink)
    assert sink in observed.event_bus._targets


def test_session_load_returns_session_with_loaded_messages(tmp_path):
    path = tmp_path / "round.jsonl"
    agent = _FakeAgent()
    session = Session(agent=agent, llm=_FakeLLM())
    session.messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    session.save(str(path))

    loaded = load_session(str(path), agent=agent, llm=_FakeLLM())

    assert loaded.messages[0]["role"] == "system"
    assert loaded.messages[0]["content"] == agent.system_prompt
    assert {"role": "user", "content": "hello"} in loaded.messages
    assert {"role": "assistant", "content": "world"} in loaded.messages


def test_session_permission_policy_setter_propagates_to_tool_processor():
    session = _new_session()
    assert session.permission_policy is None
    assert session.tool_execution.permission_policy is None

    sentinel = object()
    session.permission_policy = sentinel  # type: ignore[assignment]

    assert session.permission_policy is sentinel
    assert session.tool_execution.permission_policy is sentinel
