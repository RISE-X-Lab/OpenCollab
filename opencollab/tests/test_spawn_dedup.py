"""Single-flight spawn dedup — the reliable, tool-level guard against the
"won't stop" loop where a model re-spawns an identical task.

Prompt-level "don't duplicate" is unreliable; the scheduler enforces it. While
a (role, task) spawn is in flight, ``inflight_spawn`` reports the handling aid
and ``SpawnAgentTool`` refuses to spawn again, returning a self-describing
message instead of a second child. The reservation clears once the child
reaches a terminal phase, so a legitimate later re-run is never blocked.
"""

from __future__ import annotations

import asyncio

import pytest
from opencollab.adapters.tools.spawn import SpawnAgentTool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.tool_execution import DeferredCall, ToolRuntime
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


class BlockingChild:
    """A child whose run_loop blocks on a gate so we can observe in-flight state."""

    def __init__(self, role: str, result: str, gate: asyncio.Event):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self._result = result
        self._gate = gate

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        await self._gate.wait()
        self.state.set_phase(SessionPhase.DONE)
        self.state.append_message({"role": "assistant", "content": self._result})
        return self._result


class ChildFactory:
    def __init__(self, child: BlockingChild):
        self._child = child

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        self._child.state.aid = aid
        return self._child


def _scheduler(child: BlockingChild) -> Scheduler:
    scheduler = Scheduler(
        session_factory=ChildFactory(child),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )
    lead = BlockingChild("lead", "", asyncio.Event())
    lead.state.set_phase(SessionPhase.DONE)
    scheduler.register_lead(lead)
    return scheduler


def test_inflight_spawn_tracks_then_clears():
    async def scenario():
        gate = asyncio.Event()
        child = BlockingChild("coder", "RESULT", gate)
        scheduler = _scheduler(child)

        aid = await scheduler.spawn(0, "coder", "build it", tool_call_id="call-1")

        # In flight: the same (role, task) is reported as already handled.
        assert scheduler.inflight_spawn("coder", "build it") == aid
        # Whitespace-insensitive — re-prompting with reflowed text still collides.
        assert scheduler.inflight_spawn("coder", "build   it") == aid
        # A different task (or role) is not deduped.
        assert scheduler.inflight_spawn("coder", "build something else") is None
        assert scheduler.inflight_spawn("reviewer", "build it") is None

        gate.set()
        await scheduler._tasks[aid]

        # Terminal completion releases the reservation.
        assert scheduler.inflight_spawn("coder", "build it") is None

    run(scenario())


def test_duplicate_spawn_tool_call_is_refused_while_in_flight():
    async def scenario():
        gate = asyncio.Event()
        child = BlockingChild("coder", "RESULT", gate)
        scheduler = _scheduler(child)
        tool = SpawnAgentTool(scheduler)

        first_rt = ToolRuntime(
            environment=None, safety_policy=None, permission_policy=None,
            aid=0, tool_call_id="call-1",
        )
        deferred = await tool.execute_with_runtime(
            {"role": "coder", "task": "build it"}, first_rt
        )
        assert isinstance(deferred, DeferredCall)
        aid = deferred.ref

        # Second identical spawn returns a self-describing string, not a child.
        dup_rt = ToolRuntime(
            environment=None, safety_policy=None, permission_policy=None,
            aid=0, tool_call_id="call-2",
        )
        msg = await tool.execute_with_runtime(
            {"role": "coder", "task": "build it"}, dup_rt
        )
        assert isinstance(msg, str)
        assert f"aid={aid}" in msg
        assert "already" in msg.lower()
        # Only one child was actually spawned.
        assert len(scheduler._tasks) == 1

        gate.set()
        await scheduler._tasks[aid]

    run(scenario())


def test_model_controlled_unsafe_role_is_rejected_before_spawn_side_effects():
    async def scenario():
        gate = asyncio.Event()
        child = BlockingChild("coder", "RESULT", gate)
        scheduler = _scheduler(child)
        tool = SpawnAgentTool(scheduler)
        runtime = ToolRuntime(
            environment=None,
            safety_policy=None,
            permission_policy=None,
            aid=0,
            tool_call_id="call-unsafe",
        )

        result = await tool.execute_with_runtime(
            {"role": "../../../escaped", "task": "write outside"},
            runtime,
        )

        assert isinstance(result, str)
        assert "invalid role identity" in result
        assert scheduler._tasks == {}
        assert set(scheduler._sessions) == {0}

        with pytest.raises(ValueError, match="role"):
            await scheduler.spawn(0, "../../../escaped", "write outside")
        assert scheduler._tasks == {}
        assert set(scheduler._sessions) == {0}

    run(scenario())
