"""Session doubles and event collection shared by team lifecycle tests."""

import asyncio
import json
from typing import Any

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


async def _spawn_and_settle(scheduler, *args, **kwargs):
    """Spawn and await the resulting background task within one event loop.

    ``Scheduler.spawn`` returns after creating a detached ``_drive_agent`` task;
    awaiting that task in a *second* ``asyncio.run`` would bind it to a different
    loop (``ValueError: future belongs to a different loop``). Doing both in one
    coroutine keeps spawn and the await on the same loop.
    """
    aid = await scheduler.spawn(*args, **kwargs)
    task = scheduler._tasks.get(aid)
    if task is not None:
        await asyncio.wait_for(task, timeout=1.0)
    return aid


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
        self.state.set_phase(SessionPhase.DONE)
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
        self.built_tasks: list[tuple[str, str]] = []
        self.built_contexts: list[tuple[str, str]] = []

    def build_lead_session(self, **kwargs):
        return _FakeLeadSession()

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        self.built.append((role, budget))
        self.built_tasks.append((role, task or ""))
        self.built_contexts.append((role, context))
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
        event_sink=EventBus(sink),
    )
    lead_session = _FakeLeadSession()
    scheduler.register_lead(lead_session)
    return scheduler, captured


def _scheduler_events(events: list[Any]) -> list[SchedulerEvent]:
    return [e for e in events if isinstance(e, SchedulerEvent)]
