from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm import client as transport
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.request_admission import request_slot
from opencollab.application._session_run_shared import _TeamBudgetStop, _TokenBudgetStop
from opencollab.application.run_control import _RunCancelled
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from tests.runtime.test_llm_chat_error_usage_http import ReportedUsageStream
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("method", ["agent", "agent_run"])
@pytest.mark.parametrize("first_tokens, total, slow_ledger", [
    (5_000, 20_000, False),
    ("reduced", 20_000, False),
    (19_990, 20_000, False),
    (20_210, 20_000, False),
    (20_210, 20_000, True),
    (20_210, 20_000, "handoff"),
    (20_210, 20_000, "error_usage"),
    (20_210, None, False),
])
async def test_native_queue_rechecks_shared_workflow_budget(
    tmp_path, monkeypatch, method, first_tokens, total, slow_ledger,
):
    started, release = asyncio.Event(), asyncio.Event()
    contexts, requests, limits = [], [], []

    async def handler(request):
        body = json.loads(request.content)
        requests.append((body, contexts[0].tokens_spent()))
        if len(requests) == 1:
            started.set()
            await release.wait()
            if slow_ledger == "error_usage":
                return http_response(
                    request, 200, headers={"content-type": "text/event-stream"},
                    stream=ReportedUsageStream(
                        {"prompt_tokens": first_tokens - 10, "completion_tokens": 10, "total_tokens": first_tokens},
                        disconnect=True,
                    ),
                )
            response = completion_http_response(request, text="first answer", output_tokens=10)
            payload = response.json()
            payload["usage"].update(prompt_tokens=first_tokens - 10, total_tokens=first_tokens)
            return http_response(request, 200, json=payload)
        return completion_http_response(request, text="second answer")

    install_sdk_transport(monkeypatch, "openai", handler)
    record = transport._record_api_usage_async

    async def delayed_record(**kwargs):
        if slow_ledger is True or slow_ledger == "error_usage":
            await asyncio.sleep(0.12)
        await record(**kwargs)

    monkeypatch.setattr(transport, "_record_api_usage_async", delayed_record)
    if slow_ledger == "handoff":
        released_slot = asyncio.Event()
        native_slot = transport.request_slot
        entries = 0

        @asynccontextmanager
        async def ordered_slot(*args):
            nonlocal entries
            index = entries
            entries += 1
            if index:
                await released_slot.wait()
            async with native_slot(*args):
                yield
            if not index:
                released_slot.set()

        monkeypatch.setattr(transport, "request_slot", ordered_slot)
    pool = tmp_path / "pool"
    pool.mkdir()
    async with LLMClient(
        model="gpt-4o", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", request_pool=str(pool), request_capacity=1,
        max_retries=0,
        stream_chat=slow_ledger == "error_usage",
    ) as client:
        async def flow(ctx, _args):
            nonlocal first_tokens
            contexts.append(ctx)
            run = getattr(ctx, method)
            tasks = [asyncio.create_task(run("first", budget=18_000, tools=[]))]
            try:
                await asyncio.wait_for(started.wait(), 5)
                tasks.append(asyncio.create_task(run("second", budget=2_000, tools=[])))
                await _wait_until(lambda: len(ctx.sessions) == 2 and ctx.sessions[1].runner._llm_step_started)
                assert len(requests) == 1
                second = ctx.sessions[1]
                reserved = client.estimate_request_tokens(second.messages)
                if first_tokens == "reduced":
                    first_tokens = total - reserved - 50
                expected = min(128, (2_000 if total is None else min(2_000, total - first_tokens)) - reserved)
                limits.append(expected)
                release.set()
                values = await asyncio.wait_for(asyncio.gather(*tasks), 5)
                if expected >= 1:
                    assert getattr(values[1], "output", values[1]) == "second answer"
                    assert requests[1][0]["max_tokens"] == expected
                    assert requests[1][1] == first_tokens
                else:
                    assert getattr(values[1], "output", values[1]) == ""
                    assert "budget exhausted before model call" in second.state.terminal_reason
                return "finished"
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        result = await OpenCollab(tmp_path, model="gpt-4o", config={"max_output_tokens": 128}).workflow(
            flow, llm=client, budget=total, concurrency=2, trace=False, limit_mode="explicit",
        )
    assert result.status == "completed", result.reason
    expected_calls = 2 if limits[0] >= 1 else 1
    assert len(requests) == expected_calls
    assert result.tokens == first_tokens + (4 if expected_calls == 2 else 0)
    assert contexts[0].budget._leases == []
    assert contexts[0].pending_cleanup_tasks == ()


async def test_native_overspend_retains_requests_that_already_started(tmp_path, monkeypatch):
    first_started, second_started = asyncio.Event(), asyncio.Event()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    requests, contexts = [], []

    async def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"].splitlines()[0]
        requests.append(prompt)
        if prompt == "first":
            first_started.set()
            await release_first.wait()
            payload = completion_http_response(request, text="first answer", output_tokens=10).json()
            payload["usage"].update(prompt_tokens=20_200, total_tokens=20_210)
            return http_response(request, 200, json=payload)
        second_started.set()
        await release_second.wait()
        return completion_http_response(request, text="second answer")

    install_sdk_transport(monkeypatch, "openai", handler)
    pool = tmp_path / "pool"
    pool.mkdir()
    async with LLMClient(
        model="gpt-4o", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", request_pool=str(pool), request_capacity=2,
    ) as client:
        async def flow(ctx, _args):
            contexts.append(ctx)
            tasks = [asyncio.create_task(ctx.agent("first", tools=[], budget=18_000))]
            try:
                await asyncio.wait_for(first_started.wait(), 5)
                tasks.append(asyncio.create_task(ctx.agent("second", tools=[], budget=2_000)))
                await asyncio.wait_for(second_started.wait(), 5)
                release_first.set()
                assert await asyncio.wait_for(tasks[0], 5) == "first answer"
                assert ctx.tokens_spent() == 20_210
                assert not tasks[1].done()
                release_second.set()
                assert await asyncio.wait_for(tasks[1], 5) == "second answer"
                return "finished"
            finally:
                release_first.set()
                release_second.set()
                await asyncio.gather(*tasks, return_exceptions=True)

        result = await OpenCollab(tmp_path, model="gpt-4o", config={"max_output_tokens": 128}).workflow(
            flow, llm=client, budget=20_000, concurrency=2, trace=False, limit_mode="explicit",
        )
    assert result.status == "completed", result.reason
    assert requests == ["first", "second"]
    assert result.tokens == 20_214
    assert contexts[0].pending_cleanup_tasks == ()


@pytest.mark.parametrize("configuration, field", [
    ({"provider": "openai", "wire_protocol": "chat_completions", "model": "gpt-4o"}, "max_tokens"),
    ({"provider": "openai", "wire_protocol": "responses", "model": "gpt-4o"}, "max_output_tokens"),
    ({"provider": "anthropic", "model": "claude-sonnet-4-6"}, "max_tokens"),
], ids=["chat", "responses", "anthropic"])
@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("change", ["reduced", "exhausted", "unbounded", "cancelled", "team"])
async def test_admission_uses_current_session_limits_and_cancellation(
    tmp_path, monkeypatch, local_environment_factory, configuration, field, summary, change,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, configuration["provider"], handler)
    pool = tmp_path / "pool"
    pool.mkdir()
    team_exhausted, cancel = asyncio.Event(), asyncio.Event()
    async with LLMClient(
        **configuration, api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", request_pool=str(pool), request_capacity=1,
        max_retries=0,
    ) as client:
        session = build_session(
            agent=Agent(name="queued", system_prompt="system", max_tokens_per_step=128, **configuration),
            llm=client, env=local_environment_factory(str(tmp_path)), max_budget_tokens=20_000,
            team_budget_exhausted=team_exhausted.is_set, default_cancel_event=cancel,
        )
        messages = [{"role": "user", "content": "Finish the task."}]
        reserved = client.estimate_request_tokens(messages)
        session.messages = messages
        queued = asyncio.Event()
        native_slot = transport.request_slot

        @asynccontextmanager
        async def observed_slot(*args):
            queued.set()
            async with native_slot(*args):
                yield

        monkeypatch.setattr(transport, "request_slot", observed_slot)

        async def complete_summary(messages, *, on_response, **kwargs):
            response = await client.complete(messages, **kwargs)
            on_response(response)
            return response

        try:
            async with request_slot(str(pool), 1):
                operation = session.runner._invoke_summary(
                    complete_summary, messages, _summary_output_limit=lambda _: 80,
                ) if summary else session.run_loop()
                task = asyncio.create_task(operation)
                await asyncio.wait_for(queued.wait(), 5)
                assert requests == []
                if change == "reduced":
                    session.max_budget_tokens = reserved + 50
                elif change == "exhausted":
                    session.used_tokens = 20_000
                elif change == "unbounded":
                    session.max_budget_tokens = None
                elif change == "cancelled":
                    cancel.set()
                else:
                    team_exhausted.set()
            if summary and change in {"exhausted", "cancelled", "team"}:
                error = {"exhausted": _TokenBudgetStop, "cancelled": _RunCancelled, "team": _TeamBudgetStop}[change]
                with pytest.raises(error):
                    await asyncio.wait_for(task, 5)
            else:
                answer = await asyncio.wait_for(task, 5)
                if change in {"reduced", "unbounded"}:
                    assert getattr(answer, "content", answer) == "ok"
                else:
                    assert answer == ""
            if change in {"reduced", "unbounded"}:
                expected = 50 if change == "reduced" else (80 if summary else 128)
                assert len(requests) == 1 and requests[0][field] == expected
                assert session.used_tokens == 4
            else:
                assert requests == []
                assert session.used_tokens == (20_000 if change == "exhausted" else 0)
        finally:
            await session.aclose()


async def test_nested_team_workflow_charges_parent_before_other_native_admission(
    tmp_path, monkeypatch, local_environment_factory,
):
    from opencollab.application.event_bus import EventBus
    from opencollab.application.scheduler import Scheduler
    from opencollab.application.tool_execution import ToolRuntime
    from opencollab.application.workflow_registry import Registry, WorkflowSpec
    from opencollab.bootstrap.context_builder import SpawnConfig
    from opencollab.bootstrap.team_workflow import TeamWorkflowTool
    from opencollab.domain.scheduler import SessionControlBlock

    provider_started, release_provider, queued, ledger_started, release_ledger = (
        asyncio.Event() for _ in range(5)
    )
    requests = []

    async def reply(request):
        requests.append(json.loads(request.content))
        provider_started.set()
        await release_provider.wait()
        payload = completion_http_response(request, text="child answer").json()
        payload["usage"].update(prompt_tokens=20_198, total_tokens=20_200)
        return http_response(request, 200, json=payload)

    async def flow(ctx, _args):
        return await ctx.agent("child request", tools=[])

    native_slot, record = transport.request_slot, transport._record_api_usage_async
    entries = 0

    @asynccontextmanager
    async def observed_slot(*args):
        nonlocal entries
        entries += 1
        if entries == 2:
            queued.set()
        async with native_slot(*args):
            yield

    async def delayed_record(**kwargs):
        if kwargs["status"] == "success":
            ledger_started.set()
            await release_ledger.wait()
        await record(**kwargs)

    install_sdk_transport(monkeypatch, "openai", reply)
    monkeypatch.setattr(transport, "request_slot", observed_slot)
    monkeypatch.setattr(transport, "_record_api_usage_async", delayed_record)
    scheduler = Scheduler(session_factory=None, worktree_pool=None, event_sink=EventBus(), max_budget_tokens=20_000)
    registry = Registry()
    registry.register(WorkflowSpec(name="registered", description="Return a result", fn=flow))
    env = local_environment_factory(str(tmp_path))
    async with LLMClient(
        api_key="local-test", request_pool=str(tmp_path), request_capacity=1, max_retries=0,
    ) as client:
        parent, sibling = [
            build_session(
                env=env, llm=client, max_budget_tokens=30_000,
                agent=Agent(name=name, system_prompt="system", max_tokens_per_step=128),
                team_budget_exhausted=lambda: scheduler.budget_exhausted,
            ) for name in ("parent", "sibling")
        ]
        for aid, session in enumerate((parent, sibling)):
            scheduler.table.add(SessionControlBlock(
                aid=aid, parent_aid=0 if aid else None, agent=session.agent, state=session.state,
            ))
        config = SpawnConfig(model="gpt-4o", provider="openai", api_key=None, base_url=None,
                             llm_timeout=5, tracer=None, event_bus=EventBus(), permission_policy=None)
        tool = TeamWorkflowTool(lambda: parent, registry, config, None)
        active = asyncio.create_task(tool.execute_with_runtime(
            {"name": "registered", "args": {}}, ToolRuntime(env, None, None),
        ))
        following = None
        try:
            await asyncio.wait_for(provider_started.wait(), 5)
            await sibling.add_user_message("queued request")
            following = asyncio.create_task(sibling.run_loop())
            await asyncio.wait_for(queued.wait(), 5)
            release_provider.set()
            await asyncio.wait_for(ledger_started.wait(), 5)
            assert parent.used_tokens == scheduler.used_tokens == 20_200
            assert scheduler.budget_exhausted
            assert await asyncio.wait_for(following, 5) == ""
            assert "team budget exceeded" in sibling.state.terminal_reason
            assert not active.done()
            assert len(requests) == 1
            release_ledger.set()
            result = json.loads(await asyncio.wait_for(active, 5))
            assert result["tokens"] == parent.used_tokens == 20_200
            assert result["output"] == "child answer"
        finally:
            release_provider.set()
            release_ledger.set()
            await asyncio.gather(active, *([following] if following is not None else []), return_exceptions=True)
            await sibling.aclose()
            await parent.aclose()
