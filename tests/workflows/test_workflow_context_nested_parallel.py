"""Nested collections use current and subsequently available task slots."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.application.workflow import WorkflowContext
from tests.support.workflow_context_test_support import FakeFactory


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["direct", "parallel", "pipeline"])
async def test_nested_parallel_queue_pair_uses_available_task_slots(composition):
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=4)
    queue = asyncio.Queue(maxsize=1)

    async def producer():
        await queue.put("first")
        await queue.put("second")
        return "produced"

    async def consumer():
        return [await queue.get(), await queue.get()]

    async def group():
        return await ctx.parallel([producer, consumer])

    async def stage(_previous, _item, _index):
        return await group()

    if composition == "direct":
        operation = group()
        expected = ["produced", ["first", "second"]]
    elif composition == "parallel":
        operation = ctx.parallel([group])
        expected = [["produced", ["first", "second"]]]
    else:
        operation = ctx.pipeline(["group"], stage)
        expected = [["produced", ["first", "second"]]]

    assert await asyncio.wait_for(operation, timeout=2.0) == expected

@pytest.mark.asyncio
async def test_nested_parallel_runs_independent_units_in_available_slots():
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=4)
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def unit(index):
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running == 4:
            admitted.set()
        try:
            await release.wait()
            return index
        finally:
            running -= 1

    async def group():
        return await ctx.parallel([lambda i=i: unit(i) for i in range(4)])

    task = asyncio.create_task(ctx.parallel([group]))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=2.0)
        assert high_water == 4
    finally:
        release.set()
        result = await task

    assert result == [[0, 1, 2, 3]]

@pytest.mark.asyncio
async def test_sibling_nested_collections_use_slots_released_later():
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=4)
    queues = [asyncio.Queue(maxsize=1) for _ in range(2)]
    first_io_started = asyncio.Event()
    first_consumer_started = asyncio.Event()
    first_consumer_done = asyncio.Event()
    allow_second_group = asyncio.Event()
    second_group_started = asyncio.Event()
    second_producer_started = asyncio.Event()
    second_consumer_started = asyncio.Event()
    release_first_io = asyncio.Event()
    release_first_consumer = asyncio.Event()
    events = []
    running = high_water = 0

    async def tracked(operation):
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        try:
            return await operation()
        finally:
            running -= 1

    async def group(index):
        if index == 1:
            await allow_second_group.wait()
            second_group_started.set()

        async def producer():
            await queues[index].put("first")
            events.append((index, "first_put"))
            if index == 1:
                second_producer_started.set()
            await queues[index].put("second")
            return "produced"

        async def consumer():
            events.append((index, "consumer_started"))
            if index == 1:
                second_consumer_started.set()
            values = [await queues[index].get(), await queues[index].get()]
            if index == 0:
                first_consumer_started.set()
                await release_first_consumer.wait()
                first_consumer_done.set()
            return values

        async def io():
            if index == 0:
                first_io_started.set()
                await release_first_io.wait()
            return "io_done"

        return await ctx.parallel([
            lambda: tracked(producer),
            lambda: tracked(io),
            lambda: tracked(consumer),
        ])

    task = asyncio.create_task(ctx.parallel([lambda: group(0), lambda: group(1)]))
    try:
        await asyncio.wait_for(first_io_started.wait(), 2.0)
        await asyncio.wait_for(first_consumer_started.wait(), 2.0)
        allow_second_group.set()
        await asyncio.wait_for(second_group_started.wait(), 2.0)
        await asyncio.wait_for(second_producer_started.wait(), 2.0)
        assert (0, "consumer_started") in events
        assert (1, "first_put") in events
        assert (1, "consumer_started") not in events
        assert running == 3
        assert high_water <= 4
        release_first_io.set()
        await asyncio.wait_for(second_consumer_started.wait(), 2.0)
        assert not first_consumer_done.is_set()
    finally:
        release_first_io.set()
        release_first_consumer.set()
        allow_second_group.set()

    result = await asyncio.wait_for(task, 2.0)
    assert result == [["produced", "io_done", ["first", "second"]]] * 2
    assert (1, "consumer_started") in events
    assert running == 0
    assert high_water <= 4

@pytest.mark.asyncio
async def test_nested_parallel_cancellation_releases_extra_task_slots():
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=4)
    admitted = asyncio.Event()
    running = 0
    never = asyncio.Event()

    async def unit():
        nonlocal running
        running += 1
        if running == 4:
            admitted.set()
        try:
            await never.wait()
        finally:
            running -= 1

    async def group():
        return await ctx.parallel([unit] * 4)

    task = asyncio.create_task(ctx.parallel([group]))
    await asyncio.wait_for(admitted.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ctx.wait_for_pending_cleanup()

    assert running == 0
    assert await asyncio.wait_for(ctx.parallel([lambda: asyncio.sleep(0, result="next")] * 4), 2.0) == ["next"] * 4
    assert ctx._task_semaphore._value == 4

@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["direct", "gather", "task"])
async def test_large_sibling_nested_collections_bound_workers_and_share_task_cap(composition):
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=3)
    running = 0
    high_water = 0
    peak_tasks = 0
    admitted = asyncio.Event()
    release = asyncio.Event()
    baseline = len(asyncio.all_tasks())

    async def unit(index):
        nonlocal running, high_water, peak_tasks
        running += 1
        high_water = max(high_water, running)
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()) - baseline)
        if running == 3:
            admitted.set()
        try:
            await release.wait()
            return index
        finally:
            running -= 1

    async def group():
        operation = ctx.parallel([lambda i=i: unit(i) for i in range(2_000)])
        if composition == "gather":
            return (await asyncio.gather(operation))[0]
        if composition == "task":
            return await asyncio.create_task(operation)
        return await operation

    task = asyncio.create_task(ctx.parallel([group] * 4))
    try:
        await asyncio.wait_for(admitted.wait(), 2.0)
    finally:
        release.set()
        results = await task

    assert high_water == 3
    # Each parent slot may own an inline worker and an allocator. The
    # collection caller and admission waiter add two tasks on Python 3.10.
    wrapper_tasks = 0 if composition == "direct" else ctx._task_concurrency
    assert peak_tasks <= 3 * ctx._task_concurrency + 2 + wrapper_tasks
    assert results == [list(range(2_000))] * 4

@pytest.mark.asyncio
async def test_extra_task_slot_released_when_worker_cancelled_before_start():
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=1)
    await ctx._task_semaphore.acquire()
    worker = ctx._start_acquired_task_worker(lambda: asyncio.sleep(0))
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert await asyncio.wait_for(ctx.parallel([lambda: asyncio.sleep(0, result="next")]), 2.0) == ["next"]
    assert ctx._task_semaphore._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_ready_inherited_and_shared_slots_are_released_once(cancel):
    ctx = WorkflowContext(FakeFactory([]), task_concurrency=2)
    inherited = asyncio.Semaphore(0)
    shared = ctx._task_semaphore
    await shared.acquire()
    await shared.acquire()
    waiting = asyncio.create_task(ctx._acquire_child_task_slot(inherited))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    inherited.release()
    shared.release()
    await asyncio.sleep(0)
    if cancel:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    else:
        selected = await waiting
        selected.release()
    assert inherited._value == shared._value == 1
