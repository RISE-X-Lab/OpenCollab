from __future__ import annotations

import asyncio
import json

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


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_nested_collection_writer_finishes_before_result_delivery(tmp_path, valid):
    required = tmp_path / "metadata.txt"
    summary = tmp_path / "summary.txt"
    artifacts = tmp_path / "artifacts"
    if valid:
        required.write_text("approved")
    check_started = asyncio.Event()
    validation_finished = asyncio.Event()
    writer_started = asyncio.Event()
    writer_waiting = asyncio.Event()
    release_writer = asyncio.Event()
    children = []

    async def check():
        check_started.set()
        await validation_finished.wait()
        return "checked"

    async def write_summary(_previous, content, _index):
        writer_started.set()
        if not validation_finished.is_set():
            writer_waiting.set()
            await release_writer.wait()
        summary.write_text(content)
        return "saved"

    async def validate():
        await check_started.wait()
        try:
            await asyncio.wait_for(writer_started.wait(), 0.15)
        except asyncio.TimeoutError:
            pass
        try:
            return required.read_text()
        finally:
            validation_finished.set()

    async def flow(ctx, _inputs):
        async def combined():
            children.extend((
                asyncio.create_task(ctx.parallel([check])),
                asyncio.create_task(ctx.pipeline(["summary"], write_summary)),
            ))
            return await asyncio.gather(*children, validate())

        return await ctx.parallel([combined])

    running = asyncio.create_task(OpenCollab(tmp_path, model="offline").workflow(
        flow, task_concurrency=2, timeout=5, cleanup_timeout=2, trace=False, artifacts=artifacts,
    ))
    try:
        await asyncio.wait_for(validation_finished.wait(), 3)
        if writer_waiting.is_set():
            done, _ = await asyncio.wait({running}, timeout=0.1)
            assert not done
            assert not summary.exists()
        release_writer.set()
        result = await asyncio.wait_for(running, 3)
        assert result.status == "completed"
        assert result.output == ([[["checked"], ["saved"], "approved"]] if valid else [None])
        assert summary.read_text() == "summary"
        assert result.metrics["execution_quiesced"] is True
        manifest = json.loads((artifacts / "workflow.json").read_text())
        assert manifest["evidence_complete"] is True
    finally:
        release_writer.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, *children, return_exceptions=True)
