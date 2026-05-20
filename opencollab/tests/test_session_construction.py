"""Characterization tests for Session construction.

Lock today's observable behavior of ``Session.__init__``, ``snapshot``,
and ``load`` before Step13 moves runtime construction into
``bootstrap/container.py``.
"""

from __future__ import annotations

import asyncio
import os

from opencollab.bootstrap import build_session as Session, snapshot_session, load_session
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.compaction import ContextCompactionUseCase
from opencollab.application.session_run import SessionRunUseCase
from opencollab.adapters.storage import SessionStore
from opencollab.application.tool_execution import ToolExecutionUseCase


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


def _new_session(**overrides) -> Session:
    kwargs = dict(agent=_FakeAgent(), llm=_FakeLLM())
    kwargs.update(overrides)
    return Session(**kwargs)


def test_session_builds_runtime_collaborators_eagerly():
    session = _new_session()

    assert isinstance(session.tool_execution, ToolExecutionUseCase)
    assert isinstance(session.compactor, ContextCompactionUseCase)
    assert isinstance(session.runner, SessionRunUseCase)
    assert isinstance(session.store, SessionStore)


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
        lines = f.readlines()
    assert any('"role": "user"' in line and "hello" in line for line in lines)


def test_session_snapshot_returns_independent_session_without_autosave(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = _new_session(auto_save_path=str(path))
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


def test_session_snapshot_preserves_external_sink():
    seen: list = []

    async def sink(event):
        seen.append(event)

    session = _new_session(event_sink=sink)
    snap = snapshot_session(session)

    # External sink reference still in the bus targets.
    targets = list(snap.event_bus._targets)
    assert sink in targets


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


def test_session_with_repo_map_appends_project_structure_to_system_message():
    session = _new_session(repo_map="src/\n  app.py")
    sys_msg = session.messages[0]
    assert "Project Structure:" in sys_msg["content"]
    assert "src/" in sys_msg["content"]


def test_session_permission_policy_setter_propagates_to_tool_processor():
    session = _new_session()
    assert session.permission_policy is None
    assert session.tool_execution.permission_policy is None

    sentinel = object()
    session.permission_policy = sentinel  # type: ignore[assignment]

    assert session.permission_policy is sentinel
    assert session.tool_execution.permission_policy is sentinel
