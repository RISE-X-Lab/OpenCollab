from __future__ import annotations

import asyncio

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage


@pytest.mark.parametrize("kind", ["parallel", "pipeline", "nested"])
@pytest.mark.parametrize("start", ["overlap", "after", "explicit"])
async def test_collection_can_spend_budget_released_during_preparation(tmp_path, kind, start):
    first_started, release_first = asyncio.Event(), asyncio.Event()
    prepared, budget_released = asyncio.Event(), asyncio.Event()
    calls, rows = [], []

    class Model:
        async def complete(self, messages, **kwargs):
            prompt = messages[-1]["content"].splitlines()[0]
            calls.append(prompt)
            if prompt == "prepare source":
                first_started.set()
                await release_first.wait()
                return LLMResponse(content="prepared", usage=Usage(4990, 10))
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

        first = asyncio.create_task(ctx.agent_run("prepare source", tools=[], budget=20000))
        collection = None
        try:
            await first_started.wait()
            assert ctx.tokens_remaining() == 0
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
        tmp_path, model="test-model", config={"max_output_tokens": 128},
    ).workflow(
        flow, llm=Model(), budget=20000, concurrency=2,
        task_concurrency=2, trace=False, limit_mode="explicit", timeout=10,
    )

    assert result.status == "completed", result.reason
    assert [(row.status, row.hard_budget_tokens, row.reason) for row in rows] == [
        ("completed", 7500, None),
        ("completed", 7500, None),
    ]
    assert result.output == ["summary of first.txt", "summary of second.txt"]
    assert sorted(calls) == ["first.txt", "prepare source", "second.txt"]
    assert result.tokens == 5030
