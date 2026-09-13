"""Shared workflow environment revocation stops queued role execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from workflow_context_test_support import FakeFactory, FakeSession

from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap.workflow_runtime import WorkflowSessionFactory


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_revoked_environment_does_not_build_another_session(structured):
    factory = FakeFactory([])
    factory.environment_revoked = True
    ctx = WorkflowContext(factory)
    with pytest.raises(RuntimeError, match="environment.*revoked"):
        await ctx.agent("must not execute", schema={"type": "object"} if structured else None)
    assert factory.builds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["parallel", "pipeline"])
async def test_collection_stops_dispatch_after_environment_revocation(collection):
    factory = FakeFactory([FakeSession(reply="first"), FakeSession(reply="unexpected")])
    factory.environment_revoked = False
    ctx = WorkflowContext(factory, max_concurrency=1, task_concurrency=1)

    async def first():
        reply = await ctx.agent("first")
        factory.environment_revoked = True
        return reply

    async def second():
        return await ctx.agent("must not execute")

    with pytest.raises(RuntimeError, match="environment.*revoked"):
        if collection == "parallel":
            await ctx.parallel([first, second])
        else:

            async def stage(previous, item, index):
                return await item()

            await ctx.pipeline([first, second], stage)
    assert len(factory.builds) == 1
    assert ctx._semaphore._value == 1
    assert ctx._task_semaphore._value == 1


@pytest.mark.asyncio
async def test_queued_agent_rechecks_environment_after_waiting_for_slot():
    entered = asyncio.Event()
    release = asyncio.Event()
    factory = FakeFactory([])
    factory.environment_revoked = False

    async def revoke_on_exit():
        entered.set()
        await release.wait()
        factory.environment_revoked = True

    factory._sessions.extend([FakeSession(on_enter=revoke_on_exit), FakeSession()])
    ctx = WorkflowContext(factory, max_concurrency=1, budget_total=100_000)
    first = asyncio.create_task(ctx.agent("first", budget=1_000))
    await entered.wait()
    queued = asyncio.create_task(ctx.agent("queued", budget=1_000))
    await asyncio.sleep(0)
    release.set()
    await first
    with pytest.raises(RuntimeError, match="environment.*revoked"):
        await queued
    assert len(factory.builds) == 1
    assert ctx._semaphore._value == 1
    assert ctx.budget.remaining() == 100_000


def test_real_factory_reports_shared_environment_revocation():
    env = SimpleNamespace(revoked=False)
    factory = WorkflowSessionFactory(model="test-model", provider="openai", api_key=None, base_url=None, env=env)
    assert factory.environment_revoked is False
    env.revoked = True
    assert factory.environment_revoked is True


@pytest.mark.asyncio
async def test_revocation_during_a_tool_stops_the_same_session_before_another_model_call():
    from session_characterization_test_support import FakeAgent, FakeLLMClient, FakeTool, llm_response, tool_call

    from opencollab.adapters.env import LocalEnvironment
    from opencollab.bootstrap import build_session
    from opencollab.domain.session import SessionPhase

    env = LocalEnvironment()

    def revoke(args):
        env.revoke()
        return "Tool execution error: execution environment aborted"

    tool = FakeTool(result=revoke)
    llm = FakeLLMClient([llm_response(tool_calls=[tool_call()], finish_reason="tool_calls")])
    session = build_session(agent=FakeAgent([tool]), env=env, llm=llm)
    await session.add_user_message("do the task")
    await session.run_loop()
    assert session.state.phase is SessionPhase.STOPPED
    assert "environment has been revoked" in session.state.terminal_reason
    assert len(llm.calls) == len(tool.calls) == 1
    assert session.state.used_tokens == 2
    assert any(message.get("role") == "tool" for message in session.state.messages)
