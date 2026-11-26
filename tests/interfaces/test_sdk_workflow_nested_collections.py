from __future__ import annotations

import asyncio

import pytest

from opencollab import OpenCollab


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["direct", "gather", "task"])
@pytest.mark.parametrize("kind", ["parallel", "pipeline"])
@pytest.mark.parametrize("capacity", [2, 4])
async def test_wrapped_nested_collection_delivers_queue_output(
    tmp_path, composition, kind, capacity,
):
    delivered = tmp_path / "delivered.txt"
    events = []
    contexts = []

    async def flow(ctx, _inputs):
        contexts.append(ctx)
        queue = asyncio.Queue(maxsize=1)

        async def produce():
            events.append("producer-start")
            await queue.put("first")
            events.append("first-put")
            await queue.put("second")
            events.append("second-put")
            return "produced"

        async def consume():
            events.append("consumer-start")
            values = [await queue.get(), await queue.get()]
            delivered.write_text("\n".join(values))
            return values

        async def stage(_previous, operation, _index):
            return await operation()

        async def group():
            units = [produce, consume]
            operation = ctx.parallel(units) if kind == "parallel" else ctx.pipeline(units, stage)
            if composition == "gather":
                return (await asyncio.gather(operation))[0]
            if composition == "task":
                return await asyncio.create_task(operation)
            return await operation

        return await ctx.parallel([group])

    result = await OpenCollab(tmp_path, model="offline").workflow(
        flow, concurrency=capacity, task_concurrency=capacity, timeout=2, trace=False,
    )

    assert result.status == "completed"
    assert result.output == [["produced", ["first", "second"]]]
    assert delivered.read_text() == "first\nsecond"
    assert events == ["producer-start", "first-put", "consumer-start", "second-put"]
    assert result.metrics["execution_quiesced"] is True
    assert contexts[0]._task_semaphore._value == capacity


@pytest.mark.asyncio
@pytest.mark.parametrize("release_slot_later", [False, True])
async def test_independent_nested_collections_use_the_second_slot(tmp_path, release_slot_later):
    queue = asyncio.Queue(maxsize=1)
    filled = asyncio.Event()
    other_started = asyncio.Event()
    consumer_waiting = asyncio.Event()

    async def flow(ctx, _inputs):
        async def produce():
            if release_slot_later:
                await other_started.wait()
            await queue.put("first")
            filled.set()
            await queue.put("second")
            return "produced"

        async def consume(_previous, _item, _index):
            values = [await queue.get(), await queue.get()]
            (tmp_path / "delivered.txt").write_text("\n".join(values))
            return values

        async def receive():
            await filled.wait()
            consumer_waiting.set()
            return (await ctx.pipeline(["stream"], consume))[0]

        async def combined():
            written, read = await asyncio.gather(ctx.parallel([produce]), receive())
            return [written[0], read]

        async def other():
            other_started.set()
            await consumer_waiting.wait()
            return "finished"

        result = await ctx.parallel([combined, other] if release_slot_later else [combined])
        assert await ctx.parallel([lambda: asyncio.sleep(0, result="next")] * 2) == ["next", "next"]
        return result

    result = await OpenCollab(tmp_path, model="offline").workflow(
        flow, concurrency=2, task_concurrency=2, timeout=5, trace=False,
    )
    expected = [["produced", ["first", "second"]]]
    if release_slot_later:
        expected.append("finished")
    assert result.status == "completed"
    assert result.output == expected
    assert (tmp_path / "delivered.txt").read_text() == "first\nsecond"
