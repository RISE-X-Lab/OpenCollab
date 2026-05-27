"""Characterization tests for Scheduler event emission.

Locks the event sequence emitted by Scheduler.spawn and
Scheduler.spawn_with_review. After refactoring the spawn/review lifecycle
flows through SchedulerEvent rather than synthetic SessionEvent tool_* events.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


class _FakeTeammateSession:
    """Minimal session stand-in: records messages and returns a canned result."""

    def __init__(self, result: str, tokens: int = 0, role: str = "teammate"):
        self._result = result
        self.used_tokens = tokens
        self.added: list[str] = []
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return self._result


class _FakeLeadSession:
    """Stand-in for the Lead session; never run in these tests."""

    def __init__(self):
        self.used_tokens = 0
        self.env = None
        self.agent = type("_Agent", (), {"name": "lead"})()
        self.tool_execution = type("_TP", (), {"safety_policy": None, "env": None})()
        self.runner = type("_R", (), {"max_steps": 100})()
        self.max_steps = 100
        self.state = SessionState(messages=[])
        self.auto_save_path = None

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({"messages": self.state.enriched_messages()}, f)

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _FakeSessionFactory:
    """Drives spawn()/spawn_with_review() with canned teammate sessions."""

    def __init__(self, role_results: dict[str, list[str]]):
        # Map of role -> queue of canned results.
        self._queues = {role: list(results) for role, results in role_results.items()}
        self.built: list[tuple[str, int]] = []

    def build_lead_session(self, **kwargs):
        return _FakeLeadSession()

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None):
        self.built.append((role, budget))
        queue = self._queues.get(role, [])
        result = queue.pop(0) if queue else ""
        return _FakeTeammateSession(result, role=role)


def _build_scheduler(monkeypatch, role_results: dict[str, list[str]]) -> tuple[Scheduler, list[Any]]:
    captured: list[Any] = []

    async def sink(event):
        captured.append(event)

    factory = _FakeSessionFactory(role_results)
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=sink,
    )
    lead_session = _FakeLeadSession()
    scheduler.register_lead(lead_session)
    return scheduler, captured


def _scheduler_events(events: list[Any]) -> list[SchedulerEvent]:
    return [e for e in events if isinstance(e, SchedulerEvent)]


def test_spawn_emits_agent_spawned_then_completed(monkeypatch):
    scheduler, events = _build_scheduler(monkeypatch, {"coder": ["coder did it"]})

    result = run(scheduler.spawn(0, "coder", "do the thing"))
    # Wait for the spawned task to complete
    if result in scheduler._tasks:
        run(asyncio.wait_for(scheduler._tasks[result], timeout=1.0))

    seq = _scheduler_events(events)
    types = [e.type for e in seq]
    assert "agent_spawned" in types
    assert "agent_completed" in types

    spawned = [e for e in seq if e.type == "agent_spawned"][0]
    assert spawned.data["role"] == "coder"
    assert spawned.data["task"] == "do the thing"

    completed = [e for e in seq if e.type == "agent_completed"][0]
    assert completed.data["role"] == "coder"
    assert "latency" in completed.data
    assert isinstance(completed.data["latency"], float)


def test_spawn_autosaves_parent_tool_call(monkeypatch, tmp_path):
    scheduler, _ = _build_scheduler(monkeypatch, {"analyst": ["done"]})
    lead = scheduler.lead_session
    lead.auto_save_path = str(tmp_path / "agent_0_lead.json")
    lead.state.append_message({"role": "user", "content": "fix it"})
    lead.state.append_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "spawn_agent",
                        "arguments": '{"role": "analyst", "task": "investigate"}',
                    },
                }
            ],
        }
    )

    aid = run(scheduler.spawn(0, "analyst", "investigate"))
    run(asyncio.wait_for(scheduler._tasks[aid], timeout=1.0))

    with open(lead.auto_save_path) as f:
        saved = json.load(f)
    assert saved["messages"][-1]["tool_calls"][0]["function"]["name"] == "spawn_agent"


def test_cleanup_autosaves_live_sessions(monkeypatch, tmp_path):
    scheduler, _ = _build_scheduler(monkeypatch, {})
    lead = scheduler.lead_session
    lead.auto_save_path = str(tmp_path / "agent_0_lead.json")
    lead.state.append_message({"role": "assistant", "content": "latest state"})

    run(scheduler.cleanup())

    with open(lead.auto_save_path) as f:
        saved = json.load(f)
    assert saved["messages"][-1]["content"] == "latest state"


def test_spawn_trims_task_field_to_100_chars(monkeypatch):
    scheduler, events = _build_scheduler(monkeypatch, {"coder": [""]})
    long_task = "x" * 250

    aid = run(scheduler.spawn(0, "coder", long_task))
    if aid in scheduler._tasks:
        run(asyncio.wait_for(scheduler._tasks[aid], timeout=1.0))

    seq = _scheduler_events(events)
    spawned = [e for e in seq if e.type == "agent_spawned"][0]
    assert spawned.data["task"] == "x" * 100


def test_spawn_with_review_emits_review_lifecycle_around_spawns(monkeypatch):
    scheduler, events = _build_scheduler(
        monkeypatch,
        {
            "coder": ["implemented X"],
            "reviewer": ["Looks good.\nVERDICT: PASS"],
        },
    )

    result = run(scheduler.spawn_with_review(0, "write a function", max_iterations=3))

    assert "PASSED after 1 iteration" in result
    assert "implemented X" in result

    seq = _scheduler_events(events)
    # review_started → coder spawn → agent_completed → reviewer spawn → agent_completed → review_completed
    types = [e.type for e in seq]
    assert "review_started" in types
    assert "review_completed" in types
    assert "agent_spawned" in types
    assert "agent_completed" in types


def test_spawn_with_review_iterates_when_reviewer_fails(monkeypatch):
    scheduler, events = _build_scheduler(
        monkeypatch,
        {
            "coder": ["v1 impl", "v2 impl"],
            "reviewer": [
                "Issues found.\nVERDICT: FAIL",
                "All good.\nVERDICT: PASS",
            ],
        },
    )

    result = run(scheduler.spawn_with_review(0, "write fn", max_iterations=3))

    assert "PASSED after 2 iteration" in result
    assert "v2 impl" in result

    seq = _scheduler_events(events)
    review_loops = [e for e in seq if e.type in ("review_started", "review_completed")]
    # 2 iterations × (review_started + review_completed)
    assert [e.type for e in review_loops] == [
        "review_started",
        "review_completed",
        "review_started",
        "review_completed",
    ]
    assert review_loops[0].data["iteration"] == 1
    assert review_loops[1].data["verdict"] == "FAIL"
    assert review_loops[2].data["iteration"] == 2
    assert review_loops[3].data["verdict"] == "PASS"


def test_spawn_does_not_emit_session_tool_events(monkeypatch):
    """Scheduler.spawn must not re-use session_runtime tool_start/tool_end semantics."""
    scheduler, events = _build_scheduler(monkeypatch, {"coder": ["ok"]})

    aid = run(scheduler.spawn(0, "coder", "x"))
    if aid in scheduler._tasks:
        run(asyncio.wait_for(scheduler._tasks[aid], timeout=1.0))

    # Every event from scheduler orchestration must be a SchedulerEvent now.
    types = [getattr(e, "type", None) for e in events]
    assert "tool_start" not in types
    assert "tool_end" not in types
