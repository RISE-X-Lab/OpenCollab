"""Characterization tests for Session construction.

Lock today's observable behavior of ``Session.__init__``, ``snapshot``,
and ``load`` before Step13 moves runtime construction into
``bootstrap/container.py``.
"""

from __future__ import annotations

import asyncio
import json
import os

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
