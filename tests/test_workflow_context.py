"""Agent execution and timeout tests for WorkflowContext."""

from __future__ import annotations

import asyncio

import pytest
from workflow_context_test_support import (
    CancelCleanupSession,
    FakeFactory,
    FakeSession,
    RecordingSink,
    StubbornAddSession,
)

import opencollab.application.workflow as workflow_module
import opencollab.application.workflow_structured as workflow_structured_module
from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.application.workflow import (
    WorkflowContext,
)


@pytest.mark.parametrize("max_concurrency", [0, -1, 1.5, True, "2", float("nan")])
def test_workflow_context_rejects_invalid_concurrency(max_concurrency):
    with pytest.raises(ValueError, match="max_concurrency must be a positive integer"):
        WorkflowContext(FakeFactory([]), max_concurrency=max_concurrency)

@pytest.mark.asyncio
async def test_agent_returns_final_text_and_seeds_prompt():
    session = FakeSession(reply="the answer")
    factory = FakeFactory([session])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("solve it")

    assert result == "the answer"
    assert session.prompt == "solve it"
    assert factory.builds[0]["prompt"] == "solve it"
    assert ctx.agent_failures == ()


@pytest.mark.asyncio
async def test_agent_records_controlled_session_stop():
    session = FakeSession(reply="partial analysis")
    session.state.terminal_reason = "context overflow: prompt exceeds model window"
    ctx = WorkflowContext(FakeFactory([session]))

    assert await ctx.agent("solve it", label="coder") == "partial analysis"
    assert ctx.agent_failures == (
        {
            "label": "coder",
            "exception_type": "ContextOverflow",
            "status_code": None,
            "provider_error_type": None,
        },
    )


@pytest.mark.asyncio
async def test_agent_records_timeout_as_failure():
    session = FakeSession(gate=asyncio.Event())
    ctx = WorkflowContext(FakeFactory([session]))

    assert await ctx.agent("solve it", label="coder", timeout=0.001) is None
    assert ctx.agent_failures[0]["exception_type"] == "AgentTimeout"


@pytest.mark.asyncio
async def test_enforced_agent_records_controlled_session_stop():
    session = FakeSession(reply="partial evidence")
    session.state.terminal_reason = "context overflow: prompt exceeds model window"
    ctx = WorkflowContext(FakeFactory([session]))

    assert await ctx.agent(
        "inspect it",
        label="scout",
        enforcement_strength=ENFORCEMENT_ON,
    ) == "partial evidence"
    assert ctx.agent_failures[0]["exception_type"] == "ContextOverflow"

@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True, "bad"])
@pytest.mark.asyncio
async def test_agent_rejects_invalid_timeout_before_building_session(timeout):
    factory = FakeFactory([])
    ctx = WorkflowContext(factory)

    with pytest.raises(ValueError, match="workflow timeout"):
        await ctx.agent("must not start", timeout=timeout)

    assert factory.builds == []

@pytest.mark.asyncio
async def test_session_turn_checks_deadline_before_creating_message_coroutine(monkeypatch):
    class DeferredSession:
        message_calls = 0

        def add_user_message(self, content):
            self.message_calls += 1

            async def add():
                return None

            return add()

    session = DeferredSession()
    ctx = WorkflowContext(FakeFactory([]))

    def expired(_deadline):
        raise workflow_module.CallerTimeoutError

    monkeypatch.setattr(ctx, "_remaining_timeout", expired)

    with pytest.raises(workflow_module.CallerTimeoutError):
        await ctx._run_session_turn(session, "too late", deadline=1.0)

    assert session.message_calls == 0

@pytest.mark.asyncio
async def test_session_turn_checks_deadline_before_creating_run_loop(monkeypatch):
    class DeferredSession:
        message_calls = 0
        run_calls = 0

        def add_user_message(self, content):
            self.message_calls += 1

            async def add():
                return None

            return add()

        def run_loop(self):
            self.run_calls += 1

            async def run():
                return "done"

            return run()

    session = DeferredSession()
    ctx = WorkflowContext(FakeFactory([]))
    calls = iter((None, workflow_module.CallerTimeoutError()))

    def remaining(_deadline):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ctx, "_remaining_timeout", remaining)

    with pytest.raises(workflow_module.CallerTimeoutError):
        await ctx._run_session_turn(session, "too late", deadline=1.0)

    assert session.message_calls == 1
    assert session.run_calls == 0

@pytest.mark.asyncio
async def test_agent_error_returns_none():
    session = FakeSession(boom=True)
    ctx = WorkflowContext(FakeFactory([session]))

    assert await ctx.agent("do it") is None

@pytest.mark.asyncio
async def test_agent_error_records_safe_provider_failure_fields():
    class ProviderFailure(RuntimeError):
        status_code = 403
        body = {
            "error": {
                "message": "sensitive upstream detail",
                "type": "access_terminated_error",
            }
        }

    class ProviderFailureSession(FakeSession):
        async def run_loop(self, cancel_event=None):
            raise ProviderFailure("must not be copied into the structured record")

    ctx = WorkflowContext(FakeFactory([ProviderFailureSession()]))

    assert await ctx.agent("do it", label="solver") is None
    assert ctx.agent_failures == (
        {
            "label": "solver",
            "exception_type": "ProviderFailure",
            "status_code": 403,
            "provider_error_type": "access_terminated_error",
        },
    )

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    (
        {},
        {"enforcement_strength": ENFORCEMENT_ON},
        {"schema": {"type": "object", "properties": {}}},
    ),
)
async def test_agent_build_errors_are_recorded_for_every_public_path(kwargs):
    class ProviderFailure(RuntimeError):
        status_code = 403
        body = {"error": {"type": "access_terminated_error"}}

    class FailingFactory:
        def build_workflow_session(self, **build_kwargs):
            raise ProviderFailure("private provider message")

    ctx = WorkflowContext(FailingFactory())

    assert await ctx.agent("do it", label="solver", **kwargs) is None
    assert ctx.agent_failures[0]["provider_error_type"] == "access_terminated_error"
    assert "message" not in ctx.agent_failures[0]

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    (
        {"enforcement_strength": ENFORCEMENT_ON},
        {"schema": {"type": "object", "properties": {}}},
    ),
)
async def test_specialized_agent_run_errors_record_safe_provider_fields(kwargs):
    class ProviderFailure(RuntimeError):
        status_code = 403
        body = {"error": {"type": "access_terminated_error"}}

    class ProviderFailureSession(FakeSession):
        async def run_loop(self, cancel_event=None):
            raise ProviderFailure("private provider message")

    ctx = WorkflowContext(FakeFactory([ProviderFailureSession()]))

    await ctx.agent("do it", label="solver", **kwargs)
    assert ctx.agent_failures[0]["status_code"] == 403
    assert ctx.agent_failures[0]["provider_error_type"] == "access_terminated_error"

@pytest.mark.asyncio
async def test_structured_retry_build_error_records_safe_provider_fields():
    class ProviderFailure(RuntimeError):
        status_code = 403
        body = {"error": {"type": "access_terminated_error"}}

    class RetryFailureFactory(FakeFactory):
        def build_workflow_session(self, **kwargs):
            if self._idx:
                raise ProviderFailure("private provider message")
            return super().build_workflow_session(**kwargs)

    ctx = WorkflowContext(RetryFailureFactory([FakeSession(reply="prose")]))

    assert await ctx.agent(
        "do it",
        label="solver",
        schema={"type": "object", "properties": {}},
    ) is None
    assert ctx.agent_failures[0]["provider_error_type"] == "access_terminated_error"

@pytest.mark.asyncio
async def test_agent_forwards_tools_and_isolation():
    session = FakeSession()
    factory = FakeFactory([session])
    ctx = WorkflowContext(factory)

    await ctx.agent("p", tools=["t1"], isolation=True)

    assert factory.builds[0]["tools"] == ["t1"]
    assert factory.builds[0]["isolation"] is True

@pytest.mark.asyncio
async def test_agent_threads_tool_choice_to_factory():
    factory = FakeFactory([FakeSession(reply="ok")])
    ctx = WorkflowContext(factory)
    # Ordinary call: no tool_choice forced.
    await ctx.agent("normal")
    assert factory.builds[-1]["tool_choice"] is None
    factory._sessions.append(FakeSession(reply="ok"))
    # Forced call: tool_choice="required" reaches the factory build.
    await ctx.agent("forced", tool_choice="required")
    assert factory.builds[-1]["tool_choice"] == "required"

@pytest.mark.asyncio
async def test_agent_threads_thinking_to_factory_on_free_text_path():
    factory = FakeFactory([FakeSession(reply="ok"), FakeSession(reply="ok")])
    ctx = WorkflowContext(factory)
    # Default: thinking left None so the factory's run-wide default applies.
    await ctx.agent("normal")
    assert factory.builds[-1]["thinking"] is None
    # Forced write: thinking=False reaches the factory build (fast generation).
    await ctx.agent("forced", thinking=False)
    assert factory.builds[-1]["thinking"] is False

@pytest.mark.asyncio
async def test_agent_timeout_bounds_run_loop_and_returns_none():
    # A run_loop held open past the timeout is cancelled by asyncio.wait_for; the
    # call returns None (one dead agent never kills the fleet) and logs the timeout.
    gate = asyncio.Event()  # never set -> run_loop would block forever
    session = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await ctx.agent("slow", timeout=0.01)

    assert result is None
    assert any("timed out" in e.message for e in sink.events)

@pytest.mark.asyncio
async def test_agent_timeout_owns_stubborn_initial_message_task():
    session = StubbornAddSession()
    ctx = WorkflowContext(FakeFactory([session]))

    try:
        result = await asyncio.wait_for(
            ctx.agent("slow add", timeout=0.01),
            timeout=0.5,
        )

        assert result is None
        await asyncio.wait_for(session.cancel_seen.wait(), timeout=0.5)
        assert session.run_loop_called is False
        assert ctx.pending_cleanup_tasks
    finally:
        session.release_add.set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)

@pytest.mark.asyncio
async def test_cancelled_draft_keeps_lease_until_stubborn_message_finishes():
    stubborn = StubbornAddSession()
    second_gate = asyncio.Event()
    second = FakeSession(reply="second", gate=second_gate)
    factory = FakeFactory([stubborn, second])
    ctx = WorkflowContext(factory, budget_total=100, max_concurrency=2)

    draft_task = asyncio.create_task(
        ctx.draft_findings("draft", budget=80)
    )
    await asyncio.wait_for(stubborn.add_started.wait(), timeout=0.5)
    draft_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(draft_task, timeout=0.5)
    await asyncio.wait_for(stubborn.cancel_seen.wait(), timeout=0.5)

    second_task = asyncio.create_task(ctx.agent("second", budget=80))
    for _ in range(20):
        await asyncio.sleep(0)
        if len(factory.builds) == 2:
            break

    assert factory.builds[1]["budget"] == 20
    stubborn.release_add.set()
    second_gate.set()
    assert await second_task == "second"
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)

@pytest.mark.asyncio
async def test_draft_findings_has_internal_wall_timeout(monkeypatch):
    gate = asyncio.Event()
    session = FakeSession(reply="never", gate=gate)
    ctx = WorkflowContext(FakeFactory([session]))
    monkeypatch.setattr(
        workflow_module,
        "DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS",
        0.01,
    )

    result = await asyncio.wait_for(ctx.draft_findings("draft"), timeout=0.5)

    assert result is None
    await asyncio.wait_for(ctx.wait_for_pending_cleanup(), timeout=0.5)

@pytest.mark.asyncio
async def test_structured_agent_timeout_bounds_first_pass_and_returns_none():
    gate = asyncio.Event()
    session = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await ctx.agent(
        "slow structured",
        schema={
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string"}},
        },
        timeout=0.01,
    )

    assert result is None
    assert any("structured agent timed out" in e.message for e in sink.events)
    assert ctx.agent_failures[0]["exception_type"] == "AgentTimeout"

@pytest.mark.asyncio
async def test_structured_agent_timeout_bounds_forced_retry_and_returns_none():
    first = FakeSession(reply="prose instead of structured output")
    gate = asyncio.Event()
    retry = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([first, retry]), event_sink=sink)

    result = await ctx.agent(
        "needs structured",
        schema={
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string"}},
        },
        timeout=0.01,
    )

    assert result is None
    assert any("structured retry timed out" in e.message for e in sink.events)


@pytest.mark.asyncio
async def test_structured_retry_has_independent_short_deadline(monkeypatch):
    first = FakeSession(reply="prose instead of structured output")
    gate = asyncio.Event()
    retry = FakeSession(reply="ok", gate=gate)
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([first, retry]), event_sink=sink)
    monkeypatch.setattr(
        workflow_structured_module,
        "DEFAULT_STRUCTURED_RETRY_TIMEOUT_SECONDS",
        0.01,
    )

    result = await asyncio.wait_for(
        ctx.agent(
            "needs structured",
            schema={
                "type": "object",
                "required": ["verdict"],
                "properties": {"verdict": {"type": "string"}},
            },
            timeout=10.0,
        ),
        timeout=0.5,
    )

    assert result is None
    assert any("structured retry timed out" in e.message for e in sink.events)

@pytest.mark.asyncio
async def test_structured_agent_timeout_returns_before_cancel_cleanup_finishes():
    session = CancelCleanupSession()
    sink = RecordingSink()
    ctx = WorkflowContext(FakeFactory([session]), event_sink=sink)

    result = await asyncio.wait_for(
        ctx.agent(
            "slow structured",
            schema={
                "type": "object",
                "required": ["verdict"],
                "properties": {"verdict": {"type": "string"}},
            },
            timeout=0.01,
        ),
        timeout=0.5,
    )

    assert result is None
    assert any("structured agent timed out" in e.message for e in sink.events)
    session.release_cancel.set()
    await asyncio.sleep(0)
    assert session.cancel_seen.is_set()

@pytest.mark.asyncio
async def test_structured_provider_timeout_is_reported_as_failure_not_caller_deadline():
    class ProviderTimeoutSession(FakeSession):
        async def run_loop(self, cancel_event=None):
            raise asyncio.TimeoutError("provider transport timed out")

    sink = RecordingSink()
    ctx = WorkflowContext(
        FakeFactory([ProviderTimeoutSession()]),
        event_sink=sink,
    )

    result = await ctx.agent(
        "provider timeout",
        schema={"type": "object", "properties": {}},
        timeout=10.0,
    )

    assert result is None
    messages = [event.message for event in sink.events]
    assert any("structured agent failed" in message for message in messages)
    assert not any("structured agent timed out" in message for message in messages)

@pytest.mark.asyncio
async def test_structured_retry_keeps_caller_budget_cap():
    first = FakeSession(reply="prose", tokens=50)
    retry = FakeSession(reply="still prose", tokens=0)
    factory = FakeFactory([first, retry])
    ctx = WorkflowContext(factory, budget_total=1_000)

    await ctx.agent(
        "structured",
        schema={"type": "object", "properties": {}},
        budget=123,
    )

    assert [build["budget"] for build in factory.builds] == [123, 73]

@pytest.mark.asyncio
async def test_agent_infinite_timeout_does_not_bound_run_loop():
    # An infinite timeout (the unbounded-deadline default from seconds_left) must
    # not wrap the loop in wait_for — the call completes normally.
    session = FakeSession(reply="ok")
    ctx = WorkflowContext(FakeFactory([session]))

    result = await ctx.agent("normal", timeout=float("inf"))

    assert result == "ok"
