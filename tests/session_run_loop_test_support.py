"""Shared fakes and builders for session run-loop tests."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from opencollab.application.event_bus import EventBus
from opencollab.application.session_run import (
    SessionRunUseCase,
)
from opencollab.domain.pending import RowStatus
from opencollab.domain.session import (
    SessionState,
    TurnEnforcementState,
)
from opencollab.domain.tools import ToolProcessingResult


def run(coro):
    return asyncio.run(coro)

def llm_response(
    content=None,
    tool_calls=None,
    total_tokens=5,
    input_tokens=1,
    finish_reason="stop",
    reasoning=None,
    output_tokens=0,
):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated=False,
        ),
        finish_reason=finish_reason,
        reasoning=reasoning,
    )

def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }

class FakeAgent:
    model = "fake-model"
    temperature = 0.35

    def __init__(self, schemas=None):
        self._schemas = schemas if schemas is not None else []

    def tool_schemas(self):
        return copy.deepcopy(self._schemas)

class FakeLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "temperature": temperature,
                **kwargs,  # tool_choice / thinking ride here when set
            }
        )
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)

class FakeToolExecution:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ToolProcessingResult()

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        return self.result

class FakeToolExecutionDeferred:
    """Tool executor that also handles deferrable tools via ``execute_deferred``.

    ``deferred_outcomes`` maps a tool_call_id -> (ref, error): a non-None ref
    means the tool deferred work (returns a child aid to await); a non-None
    error means it resolved synchronously and fills its row at once.
    """

    def __init__(self, process_result=None, deferred_outcomes=None):
        self.process_calls = []
        self.deferred_calls = []
        self.process_result = process_result if process_result is not None else ToolProcessingResult()
        self.deferred_outcomes = deferred_outcomes or {}

    async def process(self, tool_calls):
        self.process_calls.append(copy.deepcopy(tool_calls))
        return self.process_result

    async def execute_deferred(self, tc):
        self.deferred_calls.append(copy.deepcopy(tc))
        return self.deferred_outcomes.get(tc["id"], (None, "no outcome"))

class CompletingBeforeReturnToolExecution(FakeToolExecutionDeferred):
    """Simulate a child that finishes before ``execute_deferred`` returns."""

    def __init__(self, state: SessionState):
        super().__init__(deferred_outcomes={"s1": (7, None)})
        self.state = state

    async def execute_deferred(self, tc):
        self.deferred_calls.append(copy.deepcopy(tc))
        row = self.state.pending_events.rows[tc["id"]]
        assert row.status is RowStatus.PENDING
        self.state.pending_events.fill(tc["id"], result="fast child result")
        return self.deferred_outcomes[tc["id"]]

class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens=0, latency=0.0):
        self.steps.append(
            {
                "step_type": step_type,
                "payload": copy.deepcopy(payload),
                "tokens": tokens,
                "latency": latency,
            }
        )

    def flush(self):
        self.flush_count += 1

def collect_events():
    events = []

    async def sink(event):
        events.append((event.type, copy.deepcopy(event.data)))

    return events, EventBus(sink)

def build_runner(
    *,
    state=None,
    agent=None,
    llm=None,
    event_bus=None,
    tool_execution=None,
    tracer=None,
    **kwargs,
):
    state = state if state is not None else SessionState(messages=[{"role": "system", "content": "sys"}])
    return SessionRunUseCase(
        agent=agent if agent is not None else FakeAgent(),
        state=state,
        llm=llm if llm is not None else FakeLLM([llm_response(content="done")]),
        event_publisher=event_bus if event_bus is not None else EventBus(None),
        tool_execution=tool_execution if tool_execution is not None else FakeToolExecution(),
        tracer=tracer,
        **kwargs,
    )

class _ToolStub:
    def __init__(self, name):
        self.name = name

def _tool_schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }

def _agent_with_tools(*names):
    agent = FakeAgent()
    agent.tools = [_ToolStub(n) for n in names]
    return agent

def _agent_with_tool_schemas(*names):
    agent = FakeAgent([_tool_schema(name) for name in names])
    agent.tools = [_ToolStub(name) for name in names]
    return agent

def _steering_steps(tracer):
    return [s for s in tracer.steps if s["step_type"] == "steering_nudge"]

def _steering_runner(reads, *, tools=("file_read", "apply_patch"), tracer=None, aid=7):
    # History ends with a tool message (not user) so the steering block is built.
    state = SessionState(
        messages=[{"role": "tool", "content": "r"}],
        used_tokens=1_000,
        step_count=3,
        turn=TurnEnforcementState(reads_since_last_edit=reads),
        aid=aid,
    )
    llm = FakeLLM([llm_response(content="done") for _ in range(8)])
    return build_runner(
        state=state,
        llm=llm,
        tracer=tracer,
        agent=_agent_with_tools(*tools),
        max_budget_tokens=100_000,
        max_steps=40,
    ), state

def _no_consecutive_same_role(messages):
    """No two adjacent messages share a role (the role-alternation contract)."""
    return all(a["role"] != b["role"] for a, b in zip(messages, messages[1:]))

def _convo():
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]

class FakeOverflowError(Exception):
    """A context-overflow provider rejection stand-in (no real SDK / network)."""

def _is_overflow(exc):
    return isinstance(exc, FakeOverflowError)

class OverflowThenOkLLM:
    """Raises a context overflow on the FIRST ``complete`` call, then succeeds.

    Records the messages it was handed each call so a test can assert the
    retried (forced-compaction) prompt is smaller than the first attempt.
    """

    def __init__(self, ok_response):
        self.ok_response = ok_response
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        if len(self.calls) == 1:
            raise FakeOverflowError("prompt is too long")
        return self.ok_response

class AlwaysOverflowLLM:
    """Raises a context overflow on EVERY ``complete`` call."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        raise FakeOverflowError("prompt is too long")

class FakeForcedShaper:
    """A shaper that no-ops on the normal pass but compacts hard when forced.

    Mirrors the real reactive layers' ``_forced`` contract: ``shape`` returns
    the messages unchanged until ``forced_shape`` flips ``_forced`` on, at which
    point it drops all but the first (pinned) message — standing in for a
    maximal compaction pass.
    """

    def __init__(self):
        self._forced = False

    def shape(self, messages):
        if not self._forced:
            return list(messages)
        return messages[:1]

class SlowLLM:
    """A provider whose ``complete`` awaits ``delay`` seconds before answering.

    Stands in for a death-slow thinking generation; with a small
    ``per_call_timeout`` the run loop must surface ``GenerationTimeoutError``.
    """

    def __init__(self, response, delay):
        self.response = response
        self.delay = delay
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(copy.deepcopy(messages))
        await asyncio.sleep(self.delay)
        return self.response

class CancelCleanupLLM:
    def __init__(self):
        self.calls = []
        self.cancel_seen = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        gate = asyncio.Event()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release_cancel.wait()
            raise
