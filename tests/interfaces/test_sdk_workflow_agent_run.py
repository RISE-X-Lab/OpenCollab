"""Public workflow sessions preserve profiles, ownership and execution results."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow import WorkflowBudgetExceeded
from opencollab.bootstrap import _workflow_runtime_session as wiring
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.tools import builtin_tools


class ReplyLLM:
    def __init__(self):
        self.calls = []
        self.closed = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), kwargs))
        return LLMResponse(content="finished", usage=Usage(4, 2), finish_reason="stop")

    def close(self):
        self.closed += 1


def client(path):
    path.mkdir(parents=True, exist_ok=True)
    return OpenCollab(path, model="test-model", config={"max_output_tokens": 128})


@pytest.mark.asyncio
async def test_real_workflow_creates_fresh_single2_sessions_and_uses_borrowed_llm(tmp_path, monkeypatch):
    llm = ReplyLLM()
    sessions = []
    build = wiring.build_session

    def capture(**kwargs):
        session = build(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(wiring, "build_session", capture)

    async def flow(ctx, _args):
        first = await ctx.agent_run("first group", tools=[], system_prompt="R6 exact prompt", max_steps=3)
        second = await ctx.agent_run("second group", tools=[], system_prompt="R6 exact prompt", max_steps=7)
        return first, second

    result = await client(tmp_path).workflow(
        flow, llm=llm, budget=10_000, concurrency=1, agent_profile="single2", trace=False,
    )
    assert result.ok
    first, second = result.output
    assert first.status == second.status == "completed"
    assert first.tokens == second.tokens == 6
    assert first.cleanup_complete and second.workspace_ready
    assert result.tokens == 12
    assert first.session_id != second.session_id
    assert [s.max_steps for s in sessions] == [3, 7]
    assert all(s.agent.system_prompt == "R6 exact prompt" for s in sessions)
    assert all(SINGLE2_SYSTEM_PROMPT not in c[0][0]["content"] for c in llm.calls)
    assert not any(m.get("content") == "first group" for m in llm.calls[1][0])
    assert llm.closed == 0


@pytest.mark.asyncio
async def test_shared_application_survives_between_real_tool_sessions(tmp_path):
    class EditingLLM(ReplyLLM):
        async def complete(self, messages, tools=None, **kwargs):
            response = await super().complete(messages, tools, **kwargs)
            if len(self.calls) == 1:
                return LLMResponse(tool_calls=[{
                    "id": "write-1", "type": "function", "function": {
                        "name": "file_write", "arguments": json.dumps({
                            "path": "shared.txt", "mode": "create", "content": "first group edit",
                        }),
                    },
                }], usage=Usage(4, 2), finish_reason="tool_calls")
            return response

    llm = EditingLLM()

    async def flow(ctx, _args):
        first = await ctx.agent_run("write the marker", tools=list(builtin_tools("file_write", headless=False)))
        assert (tmp_path / "shared.txt").read_text() == "first group edit"
        second = await ctx.agent_run("use the existing marker", tools=[])
        return first, second

    result = await client(tmp_path).workflow(flow, llm=llm, budget=10_000, agent_profile="single2")
    assert result.ok
    assert result.tokens == 18
    assert all(row.workspace_ready for row in result.output)


@pytest.mark.asyncio
async def test_explicit_finite_mode_survives_global_unbounded_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    llm = ReplyLLM()

    async def finite(ctx, _args):
        assert ctx.budget.total == 5_000
        row = await ctx.agent_run("finish", budget=2_000, max_steps=2, tools=[])
        assert row.hard_budget_tokens == 2_000
        return row

    async def unlimited(ctx, _args):
        assert ctx.budget.total is None
        return await ctx.agent_run("finish", budget=2_000, tools=[])

    limited, ordinary = await asyncio.gather(
        client(tmp_path / "finite").workflow(
            finite, llm=llm, budget=5_000, limit_mode="explicit", agent_profile="single2",
        ),
        client(tmp_path / "unlimited").workflow(unlimited, llm=ReplyLLM(), budget=5_000),
    )
    assert limited.output.tokens == 6
    assert ordinary.output.hard_budget_tokens is None


@pytest.mark.asyncio
async def test_actual_grants_bound_competing_agent_runs(tmp_path):
    entered = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    class HoldingLLM(ReplyLLM):
        async def complete(self, *args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await release.wait()
            return await super().complete(*args, **kwargs)

    async def flow(ctx, _args):
        calls = [asyncio.create_task(ctx.agent_run("work", tools=[], budget=2_000)) for _ in range(2)]
        await asyncio.wait_for(ready.wait(), 2)
        with pytest.raises(WorkflowBudgetExceeded):
            await ctx.agent_run("third", budget=100)
        release.set()
        return await asyncio.gather(*calls)

    result = await client(tmp_path).workflow(flow, llm=HoldingLLM(), budget=3_000, limit_mode="explicit")
    assert sorted(r.hard_budget_tokens for r in result.output) == [1_000, 2_000]
    assert result.tokens == 12


@pytest.mark.asyncio
async def test_stage_timeout_waits_for_late_usage_before_next_stage(tmp_path):
    class SlowFirstLLM(ReplyLLM):
        async def complete(self, messages, tools=None, **kwargs):
            if not self.calls:
                self.calls.append(([], [], {}))
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(.03)
                    return LLMResponse(content="late", usage=Usage(40, 20), finish_reason="stop")
            return await super().complete(messages, tools, **kwargs)

    async def flow(ctx, _args):
        first = await ctx.agent_run("first", tools=[], timeout=.02, cleanup_timeout=1)
        assert first.cleanup_complete and first.workspace_ready
        assert first.tokens == 60
        second = await ctx.agent_run("second", tools=[])
        return first, second

    result = await client(tmp_path).workflow(flow, llm=SlowFirstLLM(), budget=10_000)
    assert result.ok
    assert result.output[0].reason == "timeout"
    assert result.tokens == 66


@pytest.mark.asyncio
async def test_step_limit_is_reported_instead_of_success(tmp_path):
    class CallingLLM(ReplyLLM):
        async def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(tool_calls=[{
                "id": "write", "type": "function", "function": {
                    "name": "file_write",
                    "arguments": json.dumps({"path": "one.txt", "mode": "create", "content": "edit"}),
                },
            }], usage=Usage(4, 2), finish_reason="tool_calls")

    async def flow(ctx, _args):
        return await ctx.agent_run(
            "edit then stop", tools=list(builtin_tools("file_write", headless=False)), max_steps=1,
        )

    result = await client(tmp_path).workflow(flow, llm=CallingLLM(), budget=10_000, agent_profile="single2")
    assert result.output.status == "stopped"
    assert "step limit" in result.output.reason
    assert result.output.steps == 1
    assert (tmp_path / "one.txt").exists()
