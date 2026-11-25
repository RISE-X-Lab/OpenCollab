from __future__ import annotations

import asyncio
import subprocess

import pytest

from opencollab import OpenCollab
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.fs import FileReadTool


@pytest.fixture
def repository(tmp_path):
    for command in [
        ["init", "-q"],
        ["-c", "user.name=Test User", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Initialize"],
    ]:
        subprocess.run(["git", "-C", str(tmp_path), *command], check=True)
    return tmp_path


@pytest.mark.parametrize("kind", ["parallel", "pipeline"])
@pytest.mark.parametrize("method", ["agent", "agent_run", "candidate_agent"])
@pytest.mark.parametrize(
    "input_cost,output_limit,expected_grant",
    [(2800, 128, 4000), (3900, 128, 4000), (4300, 128, 4428), (2800, 8192, 4000), (2800, 65536, 4000)],
)
async def test_mixed_collection_claims_only_necessary_budget(
    repository, monkeypatch, kind, method, input_cost, output_limit, expected_grant,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    started, settle, collection_entered, collection_can_start = [asyncio.Event() for _ in range(4)]
    implicit_started, finish_implicit, explicit_done = [asyncio.Event() for _ in range(3)]
    calls, grants = [], {}

    def user_label(messages):
        return [row["content"] for row in messages if row["role"] == "user"][-1].splitlines()[0]

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return {"Prepare": 2990, "Summarize": input_cost, "Review": 20490, "Footer": 55}[user_label(messages)]

        def minimum_output_tokens(self, **kwargs):
            return 10

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            label = user_label(messages)
            calls.append((label, max_output_tokens))
            if label == "Prepare":
                started.set()
                await asyncio.wait_for(settle.wait(), 5)
            elif label == "Summarize":
                implicit_started.set()
                await asyncio.wait_for(finish_implicit.wait(), 5)
            return LLMResponse(
                content=label,
                usage=Usage(self.estimate_request_tokens(messages), 10),
                finish_reason="stop",
            )

    async def flow(ctx, _args):
        async def implicit():
            collection_entered.set()
            await asyncio.wait_for(collection_can_start.wait(), 5)
            row = await getattr(ctx, method)("Summarize", label="summary", tools=[])
            return row if method == "agent" else row.output

        async def review():
            await asyncio.wait_for(implicit_started.wait(), 5)
            try:
                grants["summary"] = ctx.sessions[-1].max_budget_tokens
                grants["available_for_review"] = ctx.tokens_remaining()
                row = await ctx.agent_run("Review", tools=[], budget=22000)
                grants["review"] = row.hard_budget_tokens
                return row.output
            finally:
                explicit_done.set()

        async def footer():
            await asyncio.wait_for(finish_implicit.wait(), 5)
            row = await ctx.agent_run("Footer", tools=[], budget=2500)
            return row.output

        async def stage(_previous, call, _index):
            return await call()

        preparer = asyncio.create_task(ctx.agent_run("Prepare", tools=[], budget=18000))
        collection = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert ctx.tokens_remaining() == 12000
            items = [implicit, review, footer]
            collection = asyncio.create_task(
                ctx.parallel(items) if kind == "parallel" else ctx.pipeline(items, stage)
            )
            await asyncio.wait_for(collection_entered.wait(), 5)
            settle.set()
            assert (await preparer).output == "Prepare"
            assert ctx.tokens_remaining() == 27000
            collection_can_start.set()
            await asyncio.wait_for(explicit_done.wait(), 5)
            finish_implicit.set()
            results = await collection
            assert ctx.budget._leases == []
            return results
        finally:
            settle.set()
            collection_can_start.set()
            finish_implicit.set()
            await asyncio.gather(preparer, *([collection] if collection else []), return_exceptions=True)

    environment = LocalEnvironment(str(repository))
    try:
        result = await OpenCollab(
            repository, environment=environment, model="offline", config={"max_output_tokens": output_limit},
        ).workflow(
            flow, llm=Model(), candidate_workspace=EnvCandidateWorkspace(environment),
            budget=30000, concurrency=3, task_concurrency=3, limit_mode="explicit",
            timeout=15, trace=False, system_prompt="Summarize the requested information.",
        )
    finally:
        await environment.cleanup()
    assert result.status == "completed", result.reason
    assert result.output == ["Summarize", "Review", "Footer"]
    assert grants == {
        "summary": expected_grant,
        "available_for_review": 27000 - expected_grant,
        "review": 22000,
    }
    assert calls == [
        ("Prepare", min(output_limit, 18000 - 2990)),
        ("Summarize", min(output_limit, expected_grant - input_cost)),
        ("Review", min(output_limit, 22000 - 20490)),
        ("Footer", min(output_limit, 2500 - 55)),
    ]
    assert result.tokens == 23575 + input_cost


@pytest.mark.parametrize("explicit_child", [False, True])
async def test_candidate_run_reclaims_for_concurrent_child_requests(repository, monkeypatch, explicit_child):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    prepare_started, release_prepare = asyncio.Event(), asyncio.Event()
    collection_started, release_collection = asyncio.Event(), asyncio.Event()
    children = {name: asyncio.Event() for name in ("first", "second")}
    release_children = asyncio.Event()
    grants = {}

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            prompt = next(row["content"] for row in reversed(messages) if row["role"] == "user")
            return 3000 if prompt in children else 256

        async def complete(self, messages, **kwargs):
            prompt = next(row["content"] for row in reversed(messages) if row["role"] == "user")
            if prompt == "prepare":
                prepare_started.set()
                await release_prepare.wait()
                return LLMResponse(content=prompt, usage=Usage(3990, 10), finish_reason="stop")
            if prompt in children:
                children[prompt].set()
                await release_children.wait()
            return LLMResponse(content=prompt, usage=Usage(5, 10), finish_reason="stop")

    async def flow(ctx, _args):
        async def candidate():
            collection_started.set()
            await release_collection.wait()

            async def child_work(child, _inputs):
                async def run(name):
                    row = await child.agent_run(name, tools=[], budget=3500 if explicit_child else None)
                    grants[name] = (row.status, row.hard_budget_tokens)
                    return row.output

                return await child.parallel([lambda: run("first"), lambda: run("second")])

            return (await ctx.candidate_workflow(child_work, {}, label="candidate")).output

        async def peer():
            try:
                for event in children.values():
                    await asyncio.wait_for(event.wait(), 5)
                assert ctx.tokens_remaining() == 16000 - 2 * 3128
                row = await ctx.agent_run("peer", budget=8000, tools=[])
                grants["peer"] = (row.status, row.hard_budget_tokens)
                return row.output
            finally:
                release_children.set()

        prepare = asyncio.create_task(ctx.agent_run("prepare", budget=16000, tools=[]))
        collection = None
        try:
            await asyncio.wait_for(prepare_started.wait(), 5)
            collection = asyncio.create_task(ctx.parallel([candidate, peer]))
            await asyncio.wait_for(collection_started.wait(), 5)
            release_prepare.set()
            assert (await prepare).output == "prepare"
            assert ctx.tokens_remaining() == 16000
            release_collection.set()
            results = await collection
            assert ctx.tokens_remaining() == 15955
            return results
        finally:
            release_prepare.set()
            release_collection.set()
            release_children.set()
            await asyncio.gather(prepare, *([collection] if collection else []), return_exceptions=True)

    environment = LocalEnvironment(str(repository))
    try:
        result = await OpenCollab(
            repository, environment=environment, model="offline", config={"max_output_tokens": 128},
        ).workflow(
            flow, llm=Model(), candidate_workspace=EnvCandidateWorkspace(environment),
            budget=20000, concurrency=3, task_concurrency=3, limit_mode="explicit", timeout=15, trace=False,
        )
    finally:
        await environment.cleanup()
    assert result.status == "completed", result.reason
    assert result.output == [["first", "second"], "peer"]
    assert grants == {"first": ("completed", 3128), "second": ("completed", 3128), "peer": ("completed", 8000)}
    assert result.tokens == 4045


@pytest.mark.parametrize("queued", [False, True])
async def test_preparer_releases_before_queued_provider_admission(tmp_path, monkeypatch, queued):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    preparing, release_prepare, finish_prepare = [asyncio.Event() for _ in range(3)]
    collection_entered = asyncio.Event()
    calls, budgets = [], {}

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return 4000 if messages[-1]["content"] == "prepare" else 7700

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            name = messages[-1]["content"]
            calls.append((name, max_output_tokens))
            if name == "prepare":
                preparing.set()
                await release_prepare.wait()
                usage = Usage(3990, 10)
            else:
                usage = Usage(7700, 10)
            return LLMResponse(content=name, usage=usage, finish_reason="stop")

    async def flow(ctx, _inputs):
        async def item(name):
            collection_entered.set()
            if not queued:
                await finish_prepare.wait()
            row = await ctx.agent_run(name, tools=[])
            budgets[name] = (row.status, row.hard_budget_tokens, row.reason)
            return row.output

        async def wait_for_reservations():
            while ctx.tokens_remaining() != 0:
                await asyncio.sleep(0)

        prepare = asyncio.create_task(ctx.agent_run("prepare", budget=19000, tools=[]))
        collection = None
        try:
            await asyncio.wait_for(preparing.wait(), 3)
            assert ctx.tokens_remaining() == 1000
            collection = asyncio.create_task(ctx.parallel([lambda: item("first"), lambda: item("second")]))
            await asyncio.wait_for(collection_entered.wait(), 3)
            if queued:
                await asyncio.wait_for(wait_for_reservations(), 3)
            release_prepare.set()
            assert (await prepare).output == "prepare"
            finish_prepare.set()
            output = await collection
            assert ctx.budget._leases == []
            return output
        finally:
            release_prepare.set()
            finish_prepare.set()
            await asyncio.gather(prepare, *([collection] if collection else []), return_exceptions=True)

    result = await OpenCollab(
        tmp_path, model="offline", config={"max_output_tokens": 128},
    ).workflow(flow, llm=Model(), budget=20000, concurrency=1, task_concurrency=2, limit_mode="explicit")
    assert result.status == "completed", result.reason
    assert result.output == ["first", "second"]
    assert calls == [("prepare", 128), ("first", 128), ("second", 128)]
    assert budgets == {"first": ("completed", 7828, None), "second": ("completed", 7828, None)}
    assert result.tokens == 19420


@pytest.mark.parametrize("exact", [False, True])
async def test_recovery_after_spending_entire_entry_share(tmp_path, monkeypatch, exact):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    (tmp_path / "notes.txt").write_text("Evidence for the review.\n")
    preparing, release_prepare = asyncio.Event(), asyncio.Event()
    collection_entered, release_collection = asyncio.Event(), asyncio.Event()
    calls = []

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            if messages[-1]["role"] == "tool":
                return 2000
            return {"prepare": 2990, "review": 5872}[messages[-1]["content"]]

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            last = messages[-1]
            if last["role"] == "tool":
                calls.append(("answer", max_output_tokens))
                assert "Evidence for the review." in last["content"]
                return LLMResponse(content="Review complete", usage=Usage(2000, 10), finish_reason="stop")
            if last["content"] == "prepare":
                calls.append(("prepare", max_output_tokens))
                preparing.set()
                await release_prepare.wait()
                return LLMResponse(content="prepared", usage=Usage(2990, 10), finish_reason="stop")
            calls.append(("read", max_output_tokens))
            return LLMResponse(
                tool_calls=[{
                    "id": "read-notes",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": '{"path":"notes.txt"}'},
                }],
                usage=Usage(5872, 128 if exact else 127),
                finish_reason="tool_calls",
            )

    async def flow(ctx, _inputs):
        async def review():
            collection_entered.set()
            await release_collection.wait()
            row = await ctx.agent_run("review", tools=[FileReadTool()])
            return row.status, row.output, row.reason, row.hard_budget_tokens

        async def record():
            return "logged"

        prepare = asyncio.create_task(ctx.agent_run("prepare", budget=18000, tools=[]))
        collection = None
        try:
            await asyncio.wait_for(preparing.wait(), 4)
            collection = asyncio.create_task(ctx.parallel([review, record]))
            await asyncio.wait_for(collection_entered.wait(), 4)
            assert ctx.tokens_remaining() == 12000
            release_prepare.set()
            assert (await prepare).output == "prepared"
            release_collection.set()
            return await collection
        finally:
            release_prepare.set()
            release_collection.set()
            await asyncio.gather(prepare, *([collection] if collection else []), return_exceptions=True)

    result = await OpenCollab(tmp_path, model="offline", config={"max_output_tokens": 128}).workflow(
        flow, llm=Model(), budget=30000, concurrency=2, task_concurrency=2, limit_mode="explicit", timeout=15
    )
    assert result.status == "completed", result.reason
    assert result.output == [("completed", "Review complete", None, 8128 if exact else 8127), "logged"]
    assert calls == [("prepare", 128), ("read", 128), ("answer", 128)]
    assert result.tokens == (11010 if exact else 11009)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("direct-after", "completed"),
        ("inner-collection", "completed"),
        ("explicit-child", "completed"),
        ("capped-child", "stopped"),
        ("capped-outer", "stopped"),
        ("direct-queued", "completed"),
    ],
)
async def test_queued_candidate_child_rechecks_parent_allocation(repository, monkeypatch, mode, expected):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    repo = repository

    def label(messages):
        return next(row["content"] for row in reversed(messages) if row["role"] == "user")

    async def wait(event):
        await asyncio.wait_for(event.wait(), 5)

    preparing, release_prepare, prepare_done = (asyncio.Event() for _ in range(3))
    child_setup = asyncio.Event()
    calls, children, views = [], [], {}

    class Model:
        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return {"prepare": 590, "review": 3000}[label(messages)]

        async def complete(self, messages, *, max_output_tokens, **kwargs):
            name = label(messages)
            calls.append((name, max_output_tokens))
            if name == "prepare":
                preparing.set()
                await wait(release_prepare)
                return LLMResponse(content=name, usage=Usage(590, 10), finish_reason="stop")
            return LLMResponse(content=name, usage=Usage(3000, 10), finish_reason="stop")

    async def child_work(child, _inputs):
        children.append(child)
        child_setup.set()
        if mode == "direct-after":
            await wait(prepare_done)

        async def review():
            cap = {"explicit-child": 4000, "capped-child": 2000}.get(mode)
            row = await child.agent_run("review", budget=cap, tools=[])
            return row.status, row.output, row.hard_budget_tokens, row.reason

        if mode == "inner-collection":
            row, = await child.parallel([review])
        else:
            row = await review()
        views["child_total"] = child.budget.total
        return row

    async def flow(ctx, _inputs):
        async def run_candidate():
            cap = 1000 if mode == "capped-outer" else None
            return (await ctx.candidate_workflow(child_work, {}, label="draft", budget=cap)).output

        async def note():
            return "noted"

        async def wait_for_child_reservation(child):
            while not child.budget._leases:
                await asyncio.sleep(0)

        prep = asyncio.create_task(ctx.agent_run("prepare", budget=19000, tools=[]))
        group = None
        try:
            await wait(preparing)
            assert ctx.tokens_remaining() == 1000
            group = asyncio.create_task(ctx.parallel([run_candidate, note]))
            await wait(child_setup)
            if mode != "direct-after":
                await asyncio.wait_for(wait_for_child_reservation(children[0]), 5)
                views["queued_grant"] = children[0].budget._leases[0].total
            views["before_release"] = ctx.tokens_remaining()
            release_prepare.set()
            await prep
            prepare_done.set()
            output = await group
            views["remaining"] = ctx.tokens_remaining()
            views["leases"] = [len(ctx.budget._leases), len(children[0].budget._leases)]
            return output
        finally:
            release_prepare.set()
            prepare_done.set()
            await asyncio.gather(*[t for t in (prep, group) if t], return_exceptions=True)

    environment = LocalEnvironment(str(repo))
    try:
        result = await OpenCollab(
            repo, environment=environment, model="offline", config={"max_output_tokens": 128},
        ).workflow(
            flow, llm=Model(), candidate_workspace=EnvCandidateWorkspace(environment),
            budget=20000, concurrency=1, task_concurrency=2, limit_mode="explicit", timeout=15, trace=False,
        )
    finally:
        await environment.cleanup()

    assert result.status == "completed", result.reason
    assert views["leases"] == [0, 0]
    assert views["remaining"] == 20000 - result.tokens
    assert result.output[0][0] == expected
    if expected == "completed":
        assert result.output[0][1] == "review"
        assert calls == [("prepare", 128), ("review", 128)]
        assert result.tokens == 3610
    else:
        assert calls == [("prepare", 128)]
        assert result.tokens == 600
        assert result.output[0][2] <= (1000 if mode == "capped-outer" else 2000)
