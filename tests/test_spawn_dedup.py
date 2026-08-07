"""Single-flight spawn dedup — the reliable, tool-level guard against the
"won't stop" loop where a model re-spawns an identical delegated task.

Prompt-level "don't duplicate" is unreliable; the scheduler enforces it. While
a (parent, role, task, context) spawn is in flight, ``inflight_spawn`` reports
the handling aid and ``SpawnAgentTool`` refuses to spawn again, returning a
self-describing message instead of a second child. The reservation clears once
the child reaches a terminal phase, so a legitimate later re-run is never
blocked.
"""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.tools.spawn import SpawnAgentTool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.scheduler_types import DuplicateSpawnError
from opencollab.application.tool_execution import DeferredCall, ToolRuntime
from opencollab.domain.scheduler import SessionControlBlock
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
    def __init__(self, children: BlockingChild | list[BlockingChild]):
        self._children = list(children) if isinstance(children, list) else [children]

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        child = self._children.pop(0)
        child.state.aid = aid
        return child


def _scheduler(child: BlockingChild | list[BlockingChild]) -> Scheduler:
    scheduler = Scheduler(
        session_factory=ChildFactory(child),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )
    lead = BlockingChild("lead", "", asyncio.Event())
    lead.state.set_phase(SessionPhase.DONE)
    scheduler.register_lead(lead)
    return scheduler


def test_inflight_spawn_keeps_parent_context_and_task_whitespace_distinct():
    async def scenario():
        gate = asyncio.Event()
        child = BlockingChild("coder", "RESULT", gate)
        scheduler = _scheduler(child)
        task = "fix:\n  return 1"
        context = "module=a"

        aid = await scheduler.spawn(0, "coder", task, context, tool_call_id="call-1")

        # Only the same parent's exact delegated input is single-flight.
        assert scheduler.inflight_spawn("coder", task, parent_aid=0, context=context) == aid
        assert scheduler.inflight_spawn("coder", task, parent_aid=1, context=context) is None
        assert scheduler.inflight_spawn("coder", task, parent_aid=0, context="module=b") is None
        assert scheduler.inflight_spawn("coder", "fix: return 1", parent_aid=0, context=context) is None
        assert scheduler.inflight_spawn("reviewer", task, parent_aid=0, context=context) is None

        gate.set()
        await scheduler._tasks[aid]

        # Terminal completion releases the reservation.
        assert scheduler.inflight_spawn("coder", task, parent_aid=0, context=context) is None

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
            {"role": "coder", "task": "build it", "context": "module=a"}, first_rt
        )
        assert isinstance(deferred, DeferredCall)
        aid = deferred.ref

        # Second identical spawn returns a self-describing string, not a child.
        dup_rt = ToolRuntime(
            environment=None, safety_policy=None, permission_policy=None,
            aid=0, tool_call_id="call-2",
        )
        msg = await tool.execute_with_runtime(
            {"role": "coder", "task": "build it", "context": "module=a"}, dup_rt
        )
        assert isinstance(msg, str)
        assert f"aid={aid}" in msg
        assert "already" in msg.lower()
        # Only one child was actually spawned.
        assert len(scheduler._tasks) == 1

        gate.set()
        await scheduler._tasks[aid]

    run(scenario())


def test_direct_duplicate_spawn_is_rejected_before_allocating_resources():
    async def scenario():
        gate = asyncio.Event()
        scheduler = _scheduler(
            [
                BlockingChild("coder", "first", gate),
                BlockingChild("coder", "duplicate", gate),
            ]
        )

        aid = await scheduler.spawn(0, "coder", "build it", "module=a")
        with pytest.raises(DuplicateSpawnError) as caught:
            await scheduler.spawn(0, "coder", "build it", "module=a")

        assert caught.value.existing_aid == aid
        assert set(scheduler._sessions) == {0, aid}
        assert set(scheduler._tasks) == {aid}
        assert set(scheduler._child_lease) == {aid}

        gate.set()
        await scheduler._tasks[aid]

    run(scenario())


def test_concurrent_direct_duplicate_spawns_create_only_one_child():
    async def scenario():
        gate = asyncio.Event()
        scheduler = _scheduler(
            [
                BlockingChild("coder", "first", gate),
                BlockingChild("coder", "duplicate", gate),
            ]
        )

        results = await asyncio.gather(
            scheduler.spawn(0, "coder", "build it", "module=a"),
            scheduler.spawn(0, "coder", "build it", "module=a"),
            return_exceptions=True,
        )

        aids = [result for result in results if isinstance(result, int)]
        duplicates = [
            result for result in results if isinstance(result, DuplicateSpawnError)
        ]
        assert len(aids) == 1
        assert len(duplicates) == 1
        assert duplicates[0].existing_aid == aids[0]
        assert set(scheduler._sessions) == {0, aids[0]}
        assert set(scheduler._tasks) == {aids[0]}
        assert set(scheduler._child_lease) == {aids[0]}

        gate.set()
        await scheduler._tasks[aids[0]]

    run(scenario())


def test_same_role_task_from_different_parent_is_not_deduped():
    async def scenario():
        gate = asyncio.Event()
        scheduler = _scheduler(
            [
                BlockingChild("coder", "first", gate),
                BlockingChild("coder", "second", gate),
            ]
        )
        second_parent = BlockingChild("reviewer", "", asyncio.Event())
        second_parent.state.aid = 2
        second_parent.state.set_phase(SessionPhase.DONE)
        scheduler.table.add(
            SessionControlBlock(
                aid=2,
                parent_aid=0,
                agent=second_parent.agent,
                state=second_parent.state,
            )
        )
        scheduler._sessions[2] = second_parent
        tool = SpawnAgentTool(scheduler)

        first = await tool.execute_with_runtime(
            {"role": "coder", "task": "inspect repository", "context": "module=a"},
            ToolRuntime(None, None, None, aid=0, tool_call_id="call-1"),
        )
        second = await tool.execute_with_runtime(
            {"role": "coder", "task": "inspect repository", "context": "module=a"},
            ToolRuntime(None, None, None, aid=2, tool_call_id="call-2"),
        )

        assert isinstance(first, DeferredCall)
        assert isinstance(second, DeferredCall)
        assert first.ref != second.ref

        gate.set()
        await asyncio.gather(*scheduler._tasks.values())

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
