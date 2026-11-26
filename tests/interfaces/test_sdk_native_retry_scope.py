"""Borrowed native connections keep independent SDK run retry allowances."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace

import openai
import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm import retry
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.retry import RetryTimeBudget
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import _workflow_runtime_session as wiring
from opencollab.environments import local_environment
from tests.runtime.test_async_compaction import history
from tests.support.provider_sdk_http import (
    completion_http_response,
    http_response,
    install_sdk_transport,
)


@dataclass
class RateLimits:
    counts: dict[str, int] = field(default_factory=dict)

    def __call__(self, request):
        body = json.loads(request.content)
        serialized = str(body)
        if "scope-sdk-warmup" in serialized:
            return completion_http_response(request, text="done")
        summary = "Your task is to create a detailed summary" in serialized
        key = "summary" if summary else re.search(r"scope-task-[a-z]+-\d+", serialized).group()
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] == 1:
            return http_response(request, 429, headers={"retry-after": "1"}, json={"error": {
                "message": "Controlled temporary rate limit.", "type": "rate_limit_error",
            }})
        return completion_http_response(request, text="<summary>Preserved context.</summary>" if summary else "done")

@pytest.fixture
def rate_limits(monkeypatch):
    monkeypatch.setattr(retry.random, "uniform", lambda *_args: 0.0)
    for name in ("OPENCOLLAB_UNBOUNDED_LIMITS", "OPENCOLLAB_API_USAGE_LOG",
                 "OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    return RateLimits()

def client(tmp_path, *, environment=None, mode="chat", allowance=1.5):
    return OpenCollab(
        tmp_path, model="fixture-model", provider="anthropic" if mode == "anthropic" else "openai",
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", environment=environment,
        config={"wire_protocol": "responses" if mode == "responses" else "chat_completions",
                "provider_error_time_budget": allowance, "llm_max_retries": 2,
                "max_output_tokens": 128, "budget": 100_000, "context_window": 40_000},
    )

async def prepare(model):
    """Initialize the official SDK before measuring failed-attempt time."""
    budget = model.provider_retry_budget
    before = None if budget is None else budget.remaining_seconds
    try:
        response = await model.complete([
            {"role": "system", "content": "Warm the controlled SDK."},
            {"role": "user", "content": "scope-sdk-warmup"},
        ], max_output_tokens=128)
        assert response.content == "done"
        assert budget is None or budget.remaining_seconds == before
        return model
    except BaseException:
        await model.close()
        raise

async def run(client, model, prompt, entry):
    if entry == "agent":
        return await client.agent(prompt, tools=(), llm=model, trace=False)

    async def flow(ctx, inputs):
        row = await ctx.agent_run(inputs["prompt"], tools=())
        if row.status != "completed":
            raise RuntimeError("Required role failed.")
        return row.output

    return await client.workflow(flow, {"prompt": prompt}, llm=model, trace=False)

@pytest.mark.parametrize("mode", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("entry", ["agent", "workflow"])
async def test_sequential_runs_recover_with_one_borrowed_connection(tmp_path, monkeypatch, rate_limits, mode, entry):
    install_sdk_transport(monkeypatch, "anthropic" if mode == "anthropic" else "openai", rate_limits)
    environment = local_environment(tmp_path)
    oc = client(tmp_path, environment=environment, mode=mode)
    model = await prepare(oc.create_model_client())
    transport = model._anthropic if mode == "anthropic" else model._openai
    try:
        results = [await run(oc, model, f"scope-task-sequential-{i}", entry) for i in range(3)]
        assert all(result.ok for result in results), ([result.status for result in results], rate_limits.counts)
        assert len({result.metrics["run_id"] for result in results}) == 3
        assert list(rate_limits.counts.values()) == [2, 2, 2]
        assert model.provider_retry_budget.remaining_seconds == 1.5
        assert not transport.is_closed()
        await environment.write_file("after.txt", "usable")
        assert await environment.read_file("after.txt") == "usable"
    finally:
        await model.close()
        await environment.cleanup()
    assert transport.is_closed()

@pytest.mark.parametrize("mode", ["chat", "responses", "anthropic"])
async def test_concurrent_agent_and_workflow_runs_keep_separate_allowances(
    tmp_path, monkeypatch, rate_limits, mode,
):
    install_sdk_transport(monkeypatch, "anthropic" if mode == "anthropic" else "openai", rate_limits)
    oc = client(tmp_path, mode=mode)
    model = await prepare(oc.create_model_client())
    try:
        results = await asyncio.gather(*[
            run(oc, model, f"scope-task-concurrent-{i}", "agent" if i % 2 == 0 else "workflow")
            for i in range(4)
        ])
        assert all(result.ok for result in results)
        assert list(rate_limits.counts.values()) == [2, 2, 2, 2]
        assert model.provider_retry_budget.remaining_seconds == 1.5
    finally:
        await model.close()

@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_workflow_roles_and_candidate_share_the_run_allowance(
    tmp_path, monkeypatch, rate_limits, candidate, parallel,
):
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    if candidate:
        (tmp_path / "marker.txt").write_text("source")
        for args in (("init", "-q"), ("add", "marker.txt"),
                     ("-c", "user.name=Kai", "-c", "user.email=kai@example.invalid", "-c", "commit.gpgsign=false",
                      "commit", "-qm", "\u521d\u59cb\u6d4b\u8bd5\u4ed3\u5e93")):
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    oc = client(tmp_path)
    model = await prepare(oc.create_model_client())

    async def second(ctx, _inputs):
        return await ctx.agent_run("scope-task-second-0", tools=(), label="reviewer")

    async def other(ctx):
        if candidate:
            return (await ctx.candidate_workflow(second, {}, label="candidate")).output
        return await second(ctx, {})

    async def flow(ctx, _inputs):
        if parallel:
            return await ctx.parallel([
                lambda: ctx.agent_run("scope-task-first-0", tools=(), label="coder"),
                lambda: other(ctx),
            ])
        first = await ctx.agent_run("scope-task-first-0", tools=(), label="coder")
        return first, await other(ctx)

    try:
        result = await oc.workflow(flow, llm=model, trace=False)
        assert result.ok
        assert [row.status for row in result.output] == ["completed", "failed"]
        assert rate_limits.counts == {"scope-task-first-0": 2, "scope-task-second-0": 1}
        followup = await run(oc, model, "scope-task-followup-0", "workflow")
        assert followup.ok
        assert rate_limits.counts["scope-task-followup-0"] == 2
    finally:
        await model.close()

async def test_summary_and_completion_share_the_workflow_allowance(tmp_path, monkeypatch, rate_limits):
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    original = wiring.build_session
    observations = []

    def seeded_session(**kwargs):
        session = original(**kwargs)
        session.messages = history()
        return session

    monkeypatch.setattr(wiring, "build_session", seeded_session)
    oc = client(tmp_path)
    model = await prepare(oc.create_model_client())

    async def flow(ctx, _inputs):
        return await ctx.agent_run(
            "scope-task-completion-0", tools=(), run_control=RunControl(on_event=observations.append),
        )

    try:
        result = await oc.workflow(flow, llm=model, trace=False)
        assert result.ok and result.output.status == "failed"
        assert rate_limits.counts == {"summary": 2, "scope-task-completion-0": 1}
        assert any(event.type == "usage" and event.data["purpose"] == "summary" for event in observations)
        assert model.provider_retry_budget.remaining_seconds == 1.5
    finally:
        await model.close()

@pytest.mark.parametrize("mode", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("first_waited", [False, True], ids=["declined-delay", "spent-time"])
async def test_denied_backoff_keeps_unused_workflow_retry_time(
    tmp_path, monkeypatch, rate_limits, mode, first_waited,
):
    allowance = 1.0

    async def handle(request):
        first = b"scope-task-first-0" in request.content
        response = rate_limits(request)
        if response.status_code == 429:
            if first and first_waited:
                await asyncio.sleep(allowance + 0.1)
            response.headers["retry-after"] = "120" if first and not first_waited else "0"
        return response

    install_sdk_transport(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handle)
    oc = client(tmp_path, mode=mode, allowance=allowance)
    model = await prepare(oc.create_model_client())

    async def flow(ctx, _inputs):
        first = await ctx.agent_run("scope-task-first-0", tools=())
        second = await ctx.agent_run("scope-task-second-0", tools=())
        return first, second

    try:
        result = await oc.workflow(flow, llm=model, trace=False, timeout=10)
        assert result.ok
        first, second = result.output
        assert (first.status, first.tokens) == ("failed", 0)
        if first_waited:
            assert (second.status, second.tokens) == ("failed", 0)
            assert result.tokens == 0
        else:
            assert (second.status, second.output, second.tokens) == ("completed", "done", 4)
            assert result.tokens == 4
        assert rate_limits.counts == {"scope-task-first-0": 1, "scope-task-second-0": 1 if first_waited else 2}
        assert model.provider_retry_budget.remaining_seconds == allowance
    finally:
        await model.close()

async def test_nested_public_run_is_independent_and_restores_the_outer_allowance(tmp_path, monkeypatch, rate_limits):
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    oc = client(tmp_path)
    model = await prepare(oc.create_model_client())

    async def flow(ctx, _inputs):
        first = await ctx.agent_run("scope-task-outer-0", tools=())
        nested = await run(oc, model, "scope-task-nested-0", "workflow")
        last = await ctx.agent_run("scope-task-outer-1", tools=())
        return first, nested, last

    try:
        result = await oc.workflow(flow, llm=model, trace=False)
        assert result.ok
        first, nested, last = result.output
        assert first.status == "completed" and nested.ok and last.status == "failed"
        assert nested.metrics["run_id"] != result.metrics["run_id"]
        assert rate_limits.counts == {"scope-task-outer-0": 2, "scope-task-nested-0": 2, "scope-task-outer-1": 1}
        assert model.provider_retry_budget.remaining_seconds == 1.5
    finally:
        await model.close()

async def test_direct_native_calls_keep_their_existing_lifetime_budget(tmp_path, monkeypatch, rate_limits):
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: 0.0))
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    oc = client(tmp_path)
    model = await prepare(oc.create_model_client())
    try:
        assert (await model.complete([{"role": "user", "content": "scope-task-direct-0"}])).content == "done"
        with pytest.raises(openai.RateLimitError):
            await model.complete([{"role": "user", "content": "scope-task-direct-1"}])
        assert model.provider_retry_budget.remaining_seconds == 0.5
        assert (await run(oc, model, "scope-task-agent-0", "agent")).ok
        with pytest.raises(openai.RateLimitError):
            await model.complete([{"role": "user", "content": "scope-task-direct-2"}])
        assert rate_limits.counts == {"scope-task-direct-0": 2, "scope-task-direct-1": 1,
                                      "scope-task-agent-0": 2, "scope-task-direct-2": 1}
    finally:
        await model.close()

@pytest.mark.parametrize("concurrent", [False, True])
async def test_explicit_shared_budget_preserves_its_cross_run_limit(tmp_path, monkeypatch, rate_limits, concurrent):
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: 0.0))
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    shared = RetryTimeBudget(1.5)
    model = await prepare(LLMClient(
        model="fixture-model", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", provider_error_time_budget=1.5, provider_retry_budget=shared,
    ))
    oc = client(tmp_path)
    try:
        if concurrent:
            results = await asyncio.gather(
                run(oc, model, "scope-task-shared-0", "agent"),
                run(oc, model, "scope-task-shared-1", "workflow"),
            )
        else:
            results = [await run(oc, model, "scope-task-shared-0", "agent"),
                       await run(oc, model, "scope-task-shared-1", "workflow")]
        assert [result.status for result in results] == ["completed", "failed"]
        assert model.provider_retry_budget is shared
        assert shared.remaining_seconds == 0.5
        assert rate_limits.counts == {"scope-task-shared-0": 2, "scope-task-shared-1": 1}
    finally:
        await model.close()

@pytest.mark.parametrize("entry", ["agent", "workflow"])
async def test_default_zero_allowance_retains_retry_count_behavior(tmp_path, monkeypatch, rate_limits, entry):
    install_sdk_transport(monkeypatch, "openai", rate_limits)
    oc = client(tmp_path, allowance=0)
    model = await prepare(oc.create_model_client())
    try:
        assert (await run(oc, model, "scope-task-zero-0", entry)).ok
        assert (await run(oc, model, "scope-task-zero-1", entry)).ok
        assert model.provider_retry_budget is None
        assert rate_limits.counts == {"scope-task-zero-0": 2, "scope-task-zero-1": 2}
    finally:
        await model.close()

async def test_third_party_model_keeps_its_existing_retry_state(tmp_path, rate_limits):
    class ThirdPartyModel:
        provider_retry_budget = RetryTimeBudget(1.5)
        closed = False

        async def complete(self, *_args, **_kwargs):
            if not self.provider_retry_budget.consume(1):
                raise RuntimeError("Third-party retry allowance exhausted.")
            return LLMResponse(content="done", usage=Usage(2, 2), finish_reason="stop")

        async def close(self):
            self.closed = True

    oc = client(tmp_path)
    model = ThirdPartyModel()
    assert (await run(oc, model, "scope-task-thirdparty-0", "agent")).ok
    assert (await run(oc, model, "scope-task-thirdparty-1", "workflow")).status == "failed"
    assert model.provider_retry_budget.remaining_seconds == 0
    assert not model.closed
    await model.close()

@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_interrupted_run_releases_its_scope_and_preserves_borrowed_resources(
    tmp_path, monkeypatch, rate_limits, ending,
):
    started = asyncio.Event()

    def handler(request):
        if "scope-task-interrupted-0" in str(json.loads(request.content)):
            started.set()
        return rate_limits(request)

    install_sdk_transport(monkeypatch, "openai", handler)
    environment = local_environment(tmp_path)
    oc = client(tmp_path, environment=environment)
    model = await prepare(oc.create_model_client())
    try:
        task = asyncio.create_task(oc.agent(
            "scope-task-interrupted-0", tools=(), llm=model, trace=False, system_prompt="Finish briefly.",
            timeout=.5 if ending == "timeout" else None,
        ))
        await asyncio.wait_for(started.wait(), 5)
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "stopped" and result.reason == "timeout"
        assert rate_limits.counts["scope-task-interrupted-0"] == 1
        assert model.provider_retry_budget.remaining_seconds == 1.5
        assert not model._openai.is_closed()
        assert not environment.revoked
        assert (await run(oc, model, "scope-task-followup-0", "workflow")).ok
        assert (await model.complete([{"role": "user", "content": "scope-task-direct-0"}])).content == "done"
        assert rate_limits.counts["scope-task-followup-0"] == rate_limits.counts["scope-task-direct-0"] == 2
        await environment.write_file("after-interruption.txt", "usable")
    finally:
        await model.close()
        await environment.cleanup()
