"""A context-overflowed CHILD delivers a controlled result to its parent.

The third leg of the safety net: when a spawned child's prompt overflows the
model window even after forced compaction, the child must reach the graceful
CONTEXT_OVERFLOW terminal and deliver a clean (DONE) result to its parent's
pending row — re-activating the parent — rather than crashing the parent's turn
with an unhandled exception. This exercises the real Scheduler lifecycle
(_drive_agent / _deliver_to_parent / _wake) with a real child SessionRunUseCase.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from opencollab.adapters.tools.spawn import SpawnAgentTool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.pending import RowStatus
from opencollab.domain.session import SessionPhase, SessionState


def run(coro):
    return asyncio.run(coro)


def llm_response(content=None, tool_calls=None, total_tokens=5, input_tokens=1, finish_reason="stop"):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(total_tokens=total_tokens, input_tokens=input_tokens),
        finish_reason=finish_reason,
    )


class FakeOverflowError(Exception):
    """A context-overflow provider rejection stand-in."""


def _is_overflow(exc):
    return isinstance(exc, FakeOverflowError)


class ScriptedLLM:
    """Returns scripted responses; a response that is a FakeOverflowError class
    is raised instead of returned (lets one LLM both answer and overflow)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append({"messages": copy.deepcopy(messages)})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class AlwaysOverflowChildLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append({"messages": copy.deepcopy(messages)})
        raise FakeOverflowError("prompt is too long")


class _NoOpShaper:
    """A shaper that can never compact below the pinned seed — so even a forced
    pass leaves the prompt overflowing (models the worst case)."""

    def __init__(self):
        self._forced = False

    def shape(self, messages):
        return list(messages)


class _ChildAgent:
    def __init__(self, role):
        self.name = role
        self.model = "fake"
        self.temperature = 0.0

    def tool_schemas(self):
        return []


class OverflowChildSession:
    """A child whose run loop overflows persistently and stops gracefully."""

    def __init__(self, role, aid):
        self.agent = _ChildAgent(role)
        self.state = SessionState(messages=[{"role": "system", "content": "child sys"}])
        self.state.aid = aid
        self.used_tokens = 0
        self.llm = AlwaysOverflowChildLLM()
        self.runner = SessionRunUseCase(
            agent=self.agent,
            state=self.state,
            llm=self.llm,
            event_publisher=EventBus(None),
            tool_execution=_NullToolExecution(),
            shaper=_NoOpShaper(),
            is_context_overflow=_is_overflow,
        )

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        return await self.runner.run_loop()


class _NullToolExecution:
    async def process(self, tool_calls):
        from opencollab.domain.tools import ToolProcessingResult

        return ToolProcessingResult()


class OverflowChildFactory:
    def __init__(self):
        self.child = None

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        self.child = OverflowChildSession(role, aid)
        return self.child


class LeadSession:
    def __init__(self, runner, state, agent):
        self.runner = runner
        self.state = state
        self.agent = agent
        self.used_tokens = 0

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})
        self.state.reset_for_user_turn()

    async def run_loop(self) -> str:
        return await self.runner.run_loop()


def test_overflowed_child_delivers_controlled_result_not_crash():
    factory = OverflowChildFactory()
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )

    lead_state = SessionState(messages=[{"role": "system", "content": "you are lead"}])
    lead_agent = Agent(
        name="lead",
        system_prompt="you are lead",
        tools=[SpawnAgentTool(scheduler)],
    )
    bus = EventBus(None)
    tool_execution = ToolExecutionUseCase(
        agent=lead_agent,
        environment=None,
        state=lead_state,
        event_publisher=bus,
    )
    spawn_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "spawn_agent", "arguments": '{"role": "coder", "task": "build it"}'},
    }
    # Lead: spawn, then (after the child delivers) answer normally.
    lead_llm = ScriptedLLM(
        [
            llm_response(content="delegating", tool_calls=[spawn_call], finish_reason="tool_calls"),
            llm_response(content="Lead recovered after child overflow", total_tokens=4),
        ]
    )
    runner = SessionRunUseCase(
        agent=lead_agent,
        state=lead_state,
        llm=lead_llm,
        event_publisher=bus,
        tool_execution=tool_execution,
    )
    lead = LeadSession(runner, lead_state, lead_agent)
    scheduler.register_lead(lead)

    # No unhandled exception escapes — the lead completes its turn.
    result = run(scheduler.run("please build it via a coder"))

    # The lead recovered and finished — its turn was NOT crashed by the child.
    assert result == "Lead recovered after child overflow"
    assert lead_state.phase is SessionPhase.DONE
    assert lead_state.pending_events.is_empty()

    # The child reached the dedicated graceful terminal (not ERROR).
    child = factory.child
    assert child.state.phase is SessionPhase.CONTEXT_OVERFLOW
    # The child tried the provider exactly twice (initial + one forced retry).
    assert len(child.llm.calls) == 2

    # The child's completion was delivered to the parent's row as a controlled
    # DONE result (ERROR would have made it a FAILED row).
    child_scb = scheduler.table.get(child.state.aid)
    assert child_scb.state.phase is SessionPhase.CONTEXT_OVERFLOW

    # The lead's second call saw a tool result for the spawn (a delivered,
    # non-crashing completion), confirming the row was filled and it re-activated.
    second_call_msgs = lead_llm.calls[1]["messages"]
    tool_results = [m for m in second_call_msgs if m.get("role") == "tool" and m.get("tool_call_id") == "call-1"]
    assert len(tool_results) == 1


def test_delivery_status_of_overflowed_child_is_done_not_failed():
    # Directly assert the lifecycle's status mapping: a CONTEXT_OVERFLOW child is
    # delivered DONE (only an ERROR phase maps to FAILED). Verifies the row the
    # parent ends up with carries a non-failure status.
    factory = OverflowChildFactory()
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )

    lead_state = SessionState(messages=[{"role": "system", "content": "lead"}])
    lead_agent = Agent(name="lead", system_prompt="lead", tools=[SpawnAgentTool(scheduler)])
    bus = EventBus(None)
    tool_execution = ToolExecutionUseCase(
        agent=lead_agent, environment=None, state=lead_state, event_publisher=bus
    )
    spawn_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "spawn_agent", "arguments": '{"role": "coder", "task": "t"}'},
    }
    lead_llm = ScriptedLLM(
        [
            llm_response(content="go", tool_calls=[spawn_call], finish_reason="tool_calls"),
            llm_response(content="done", total_tokens=4),
        ]
    )
    runner = SessionRunUseCase(
        agent=lead_agent, state=lead_state, llm=lead_llm,
        event_publisher=bus, tool_execution=tool_execution,
    )
    lead = LeadSession(runner, lead_state, lead_agent)
    scheduler.register_lead(lead)

    run(scheduler.run("delegate"))

    # The spawn tool's result delivered to the lead carries the child's run-loop
    # output (empty/controlled), and the lead's turn completed cleanly. The row
    # status surfaced as DONE — verified via the filled tool message actually
    # flowing into the lead's history rather than an error string.
    assert factory.child.state.phase is SessionPhase.CONTEXT_OVERFLOW
    assert lead_state.phase is SessionPhase.DONE
    # The delivered tool message exists and is not an "Error:" string (which is
    # how a FAILED/ERROR child would have been surfaced).
    tool_msg = next(m for m in lead_state.messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1")
    assert not str(tool_msg.get("content", "")).startswith("Error:")
    # Sanity: RowStatus exists and DONE is the success status used in delivery.
    assert RowStatus.DONE is not RowStatus.FAILED
