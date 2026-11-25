from __future__ import annotations

import asyncio

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage


@pytest.mark.parametrize("kind", ["parallel", "pipeline", "nested"])
@pytest.mark.parametrize("start", ["overlap", "after", "explicit"])
@pytest.mark.parametrize("first_reservation", [20000, 19999])
async def test_collection_can_spend_budget_released_during_preparation(tmp_path, kind, start, first_reservation):
    first_started, release_first = asyncio.Event(), asyncio.Event()
    prepared, budget_released = asyncio.Event(), asyncio.Event()
    calls, rows = [], []
    input_tokens, output_limit = 256, 128

    class Model:
        def estimate_request_tokens(self, messages, tools, **kwargs):
            return input_tokens

        async def complete(self, messages, **kwargs):
            prompt = messages[-1]["content"].splitlines()[0]
            calls.append(prompt)
            if prompt == "prepare source":
                first_started.set()
                await release_first.wait()
                return LLMResponse(content="prepared", usage=Usage(4990, 10))
            assert kwargs["max_output_tokens"] == output_limit
            return LLMResponse(content=f"summary of {prompt}", usage=Usage(5, 10))

    async def flow(ctx, _inputs):
        async def summarize(name):
            prepared.set()
            await budget_released.wait()
            row = await ctx.agent_run(name, tools=[], budget=7500 if start == "explicit" else None)
            rows.append(row)
            return row.output

        async def stage(_previous, name, _index):
            return await summarize(name)

        async def collect():
            if kind == "pipeline":
                return await ctx.pipeline(["first.txt", "second.txt"], stage)
            if kind == "nested":
                async def group():
                    return await ctx.pipeline(["first.txt", "second.txt"], stage)

                return (await ctx.parallel([group]))[0]
            return await ctx.parallel([
                lambda: summarize("first.txt"),
                lambda: summarize("second.txt"),
            ])

        first = asyncio.create_task(ctx.agent_run("prepare source", tools=[], budget=first_reservation))
        collection = None
        try:
            await first_started.wait()
            assert ctx.tokens_remaining() == 20000 - first_reservation
            if start != "after":
                collection = asyncio.create_task(collect())
                await prepared.wait()
            release_first.set()
            assert (await first).output == "prepared"
            assert ctx.tokens_remaining() == 15000
            budget_released.set()
            return await (collection if collection is not None else collect())
        finally:
            release_first.set()
            budget_released.set()
            await asyncio.gather(first, *([collection] if collection else []), return_exceptions=True)

    result = await OpenCollab(
        tmp_path, model="test-model", config={"max_output_tokens": output_limit},
    ).workflow(
        flow, llm=Model(), budget=20000, concurrency=2,
        task_concurrency=2, trace=False, limit_mode="explicit", timeout=10,
    )

    assert result.status == "completed", result.reason
    grant = input_tokens + output_limit if start == "overlap" else 7500
    assert [(row.status, row.hard_budget_tokens, row.reason) for row in rows] == [
        ("completed", grant, None),
        ("completed", grant, None),
    ]
    assert result.output == ["summary of first.txt", "summary of second.txt"]
    assert sorted(calls) == ["first.txt", "prepare source", "second.txt"]
    assert result.tokens == 5030

@pytest.mark.parametrize("kind", ["parallel", "pipeline", "nested"])
@pytest.mark.parametrize("explicit_first", [False, True])
async def test_explicit_peer_preserves_collection_share(tmp_path, monkeypatch, kind, explicit_first):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    started, release = asyncio.Event(), asyncio.Event()
    calls, rows = [], {}
    prepare, review = "Preparation", "Read the following notes."
    first_prompt = prepare if explicit_first else review
    notes = review + "\n" + " report" * 2800

    class Model:
        async def complete(self, messages, **kwargs):
            prompt = messages[-1]["content"].splitlines()[0]
            calls.append(prompt)
            if prompt == first_prompt:
                started.set()
                await release.wait()
            return LLMResponse(content=prompt, usage=Usage(8, 7))

    async def flow(ctx, _inputs):
        async def run_item(item):
            explicit = item == prepare
            first = explicit == explicit_first
            if not first:
                await asyncio.wait_for(started.wait(), 5)
                assert ctx.tokens_remaining() == (8000 if explicit_first else 10000)
            try:
                row = await ctx.agent_run(item, tools=[], budget=12000 if explicit else None)
                rows["prepare" if explicit else "review"] = row
                return row.output
            finally:
                if not first:
                    release.set()

        async def stage(_previous, item, _index):
            return await run_item(item)

        async def pipeline():
            return await ctx.pipeline([prepare, notes], stage)

        if kind == "nested":
            return (await ctx.parallel([pipeline]))[0]
        if kind == "pipeline":
            return await pipeline()
        return await ctx.parallel([lambda: run_item(prepare), lambda: run_item(notes)])

    result = await OpenCollab(
        tmp_path, model="test-model", config={"max_output_tokens": 128},
    ).workflow(
        flow, llm=Model(), budget=20000, concurrency=2, task_concurrency=2,
        timeout=10, trace=False, limit_mode="explicit", system_prompt="Summarize one detail.",
    )

    grants = [12000, 8000] if explicit_first else [10000, 10000]
    results = [rows["prepare"], rows["review"]]
    assert result.status == "completed", result.reason
    assert [row.status for row in results] == ["completed", "completed"]
    assert [row.hard_budget_tokens for row in results] == grants
    assert result.output == [prepare, review]
    assert sorted(calls) == sorted([prepare, review])
    assert result.tokens == 30

@pytest.mark.parametrize("hold_external", [False, True], ids=["already-released", "still-reserved"])
async def test_collection_keeps_live_available_budget_during_later_temporary_reservation(
    tmp_path, monkeypatch, hold_external,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    preparing, admit_collection = asyncio.Event(), asyncio.Event()
    external_started, release_external, external_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_finished = asyncio.Event()
    prompt = "Summarize the detailed report.\n" + " report" * 2800
    grants = {}

    class Model:
        async def complete(self, messages, **kwargs):
            request = [row["content"] for row in messages if row["role"] == "user"][-1].splitlines()[0]
            if request == "Annotate the other document.":
                external_started.set()
                await release_external.wait()
            return LLMResponse(content=request, usage=Usage(8, 7))

    async def flow(ctx, _inputs):
        async def first():
            preparing.set()
            await admit_collection.wait()
            try:
                row = await ctx.agent_run(prompt, tools=[])
                grants["first"] = row
                return row.output
            finally:
                first_finished.set()

        async def second():
            await external_finished.wait()
            row = await ctx.agent_run("Summarize the short report.", tools=[])
            grants["second"] = row
            return row.output

        async def outside_collection():
            row = await ctx.agent_run("Annotate the other document.", tools=[], budget=12000)
            grants["external"] = row
            external_finished.set()
            return row.output

        collection = asyncio.create_task(ctx.parallel([first, second]))
        outside = None
        try:
            await asyncio.wait_for(preparing.wait(), 4)
            assert ctx.tokens_remaining() == 20000
            outside = asyncio.create_task(outside_collection())
            await asyncio.wait_for(external_started.wait(), 4)
            assert ctx.tokens_remaining() == 8000
            if not hold_external:
                release_external.set()
                await outside
            admit_collection.set()
            await asyncio.wait_for(first_finished.wait(), 4)
            release_external.set()
            await outside
            return await collection
        finally:
            admit_collection.set()
            release_external.set()
            external_finished.set()
            await asyncio.gather(collection, *([outside] if outside is not None else []), return_exceptions=True)

    result = await OpenCollab(
        tmp_path, model="offline", config={"max_output_tokens": 128},
    ).workflow(
        flow, llm=Model(), budget=20000, concurrency=2, task_concurrency=2, timeout=10,
        trace=False, limit_mode="explicit", system_prompt="Summarize one detail.",
    )
    assert result.status == "completed", result.reason
    assert result.output == ["Summarize the detailed report.", "Summarize the short report."]
    assert result.tokens == 45
    assert all(row.status == "completed" for row in grants.values())
    if hold_external:
        assert grants["first"].hard_budget_tokens == 8000
    else:
        assert 9900 <= grants["first"].hard_budget_tokens <= 10000

@pytest.mark.parametrize("nested", [False, True], ids=["direct", "nested"])
async def test_parent_balance_uses_its_own_entry_and_live_admission(tmp_path, monkeypatch, nested):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")

    preparation_started, settle_preparation = asyncio.Event(), asyncio.Event()
    parent_preparing, start_first_group = asyncio.Event(), asyncio.Event()
    first_group_entered, admit_first = asyncio.Event(), asyncio.Event()
    outside_started, settle_outside = asyncio.Event(), asyncio.Event()
    first_started, finish_first = asyncio.Event(), asyncio.Event()
    second_attempted = asyncio.Event()
    grants = {}

    class Model:
        def estimate_request_tokens(self, messages, tools, **kwargs):
            prompt = [row["content"] for row in messages if row["role"] == "user"][-1]
            return 3999 if prompt.startswith("Summarize the") else 256

        async def complete(self, messages, **kwargs):
            prompt = [row["content"] for row in messages if row["role"] == "user"][-1]
            if prompt == "Prepare a set of notes.":
                preparation_started.set()
                await settle_preparation.wait()
                return LLMResponse(content=prompt, usage=Usage(3990, 10))
            if prompt == "Annotate the unrelated document.":
                outside_started.set()
                await settle_outside.wait()
            elif prompt == "Summarize the first report.":
                first_started.set()
                await finish_first.wait()
            return LLMResponse(content=prompt, usage=Usage(8, 7))

    async def flow(ctx, _inputs):
        async def first_item():
            assert ctx.tokens_remaining() == 16000
            first_group_entered.set()
            await admit_first.wait()
            row = await ctx.agent_run("Summarize the first report.", tools=[])
            grants["first"] = row.hard_budget_tokens
            return row.output

        async def stage(_previous, _item, _index):
            return await first_item()

        async def first_group():
            assert ctx.tokens_remaining() == 4000
            parent_preparing.set()
            await start_first_group.wait()
            if nested:
                return (await ctx.pipeline(["first"], stage))[0]
            return await first_item()

        async def second_group():
            await first_started.wait()
            assert ctx.tokens_remaining() == 4000
            try:
                row = await ctx.agent_run("Summarize the second report.", tools=[])
                grants["second"] = row.hard_budget_tokens
                return row.output
            finally:
                second_attempted.set()

        first = asyncio.create_task(ctx.agent_run("Prepare a set of notes.", tools=[], budget=16000))
        tasks = [first]
        try:
            await asyncio.wait_for(preparation_started.wait(), 4)
            assert ctx.tokens_remaining() == 4000
            collection = asyncio.create_task(ctx.parallel([first_group, second_group]))
            tasks.append(collection)
            await asyncio.wait_for(parent_preparing.wait(), 4)
            settle_preparation.set()
            assert (await first).output == "Prepare a set of notes."
            assert ctx.tokens_remaining() == 16000
            start_first_group.set()
            await asyncio.wait_for(first_group_entered.wait(), 4)
            other = asyncio.create_task(ctx.agent_run("Annotate the unrelated document.", tools=[], budget=8000))
            tasks.append(other)
            await asyncio.wait_for(outside_started.wait(), 4)
            assert ctx.tokens_remaining() == 8000
            admit_first.set()
            await asyncio.wait_for(second_attempted.wait(), 4)
            finish_first.set()
            settle_outside.set()
            await other
            return await collection
        finally:
            settle_preparation.set()
            start_first_group.set()
            admit_first.set()
            finish_first.set()
            settle_outside.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    result = await OpenCollab(
        tmp_path, model="offline", config={"max_output_tokens": 128},
    ).workflow(
        flow, llm=Model(), budget=20000, concurrency=3, task_concurrency=3, timeout=10,
        trace=False, limit_mode="explicit", system_prompt="Summarize one detail.",
    )
    assert result.status == "completed", result.reason
    assert result.output == ["Summarize the first report.", "Summarize the second report."]
    assert result.tokens == 4045
    assert grants == {"first": 4000, "second": 4000}
