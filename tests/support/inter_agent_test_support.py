"""Session and scheduler preparation shared by inter-agent tests."""

import asyncio

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    """Session stand-in: run_loop returns canned results from a queue."""

    def __init__(self, results, role):
        self._results = list(results)
        self.added: list[str] = []
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return self._results.pop(0) if self._results else ""


class FakeFactory:
    def __init__(self, teammate):
        self._teammate = teammate

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        return self._teammate


def _build_scheduler(teammate, topology=None, roles=()):
    captured: list = []

    async def sink(event):
        captured.append(event)

    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        topology=topology,
        roles=roles,
    )
    lead = FakeSession([], role="lead")
    scheduler.register_lead(lead)
    return scheduler, captured
