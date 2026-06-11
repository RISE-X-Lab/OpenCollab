"""End-to-end: real run loop + real SpawnAgentTool + real Scheduler.

Proves the central behavior change — a lead that calls ``spawn_agent`` suspends
on AWAITING_EVENTS, the child runs, and the lead is re-activated to reason over
the child's result in the SAME turn (delivered as the spawn tool call's result),
rather than answering before the child finishes.
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


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append({"messages": copy.deepcopy(messages)})
        return self.responses.pop(0)


class ChildSession:
    """A scripted child: its run_loop finishes terminal with a canned result."""

    def __init__(self, role, result):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self._result = result

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        self.state.append_message({"role": "assistant", "content": self._result})
        return self._result


class ChildFactory:
    def __init__(self, child):
        self._child = child

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        self._child.state.aid = aid
        return self._child


class LeadSession:
    """Adapter exposing the real SessionRunUseCase as a scheduler-driven session."""

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


def test_lead_reasons_over_child_result_in_same_turn():
    child = ChildSession("coder", "THE CHILD RESULT")
    scheduler = Scheduler(
        session_factory=ChildFactory(child),
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
    llm = FakeLLM(
        [
            llm_response(content="delegating", tool_calls=[spawn_call], finish_reason="tool_calls"),
            llm_response(content="Based on the child: done", total_tokens=4),
        ]
    )
    runner = SessionRunUseCase(
        agent=lead_agent,
        state=lead_state,
        llm=llm,
        event_publisher=bus,
        tool_execution=tool_execution,
    )
    lead = LeadSession(runner, lead_state, lead_agent)
    scheduler.register_lead(lead)

    result = run(scheduler.run("please build it via a coder"))

    # The final answer is the lead's POST-child reasoning, not its pre-spawn text.
    assert result == "Based on the child: done"
    assert lead_state.phase is SessionPhase.DONE
    assert lead_state.pending_events.is_empty()

    # The second LLM call saw the child's result as the spawn tool call's result.
    second_call_msgs = llm.calls[1]["messages"]
    assert {"role": "tool", "tool_call_id": "call-1", "content": "THE CHILD RESULT"} in second_call_msgs

    # The child actually ran and was tracked.
    assert child.state.phase is SessionPhase.DONE
    assert scheduler.table.get(child.state.aid).result == "THE CHILD RESULT"
