from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from opencollab.application.async_timeout import (
    abandon_on_timeout,
    cancel_tasks_and_wait,
    force_task_terminal,
    run_with_bounded_shutdown,
)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "bad"])
def test_abandon_on_timeout_rejects_unbounded_numeric_sentinels(timeout):
    async def scenario() -> None:
        future = asyncio.get_running_loop().create_future()
        future.set_result("done")
        with pytest.raises(ValueError, match="finite positive"):
            await abandon_on_timeout(future, timeout)

    asyncio.run(scenario())


def test_abandon_on_timeout_allows_none_as_explicit_disable():
    async def scenario() -> str:
        future = asyncio.get_running_loop().create_future()
        future.set_result("done")
        return await abandon_on_timeout(future, None)

    assert asyncio.run(scenario()) == "done"


@pytest.mark.parametrize(
    "shutdown_timeout",
    [0, -1, float("nan"), float("inf"), True, None, "bad"],
)
def test_bounded_shutdown_rejects_invalid_timeout_before_event_loop(
    shutdown_timeout,
):
    with pytest.raises(ValueError, match="finite positive"):
        run_with_bounded_shutdown(None, shutdown_timeout=shutdown_timeout)


def test_force_task_terminal_finishes_coroutine_that_consumes_cancel():
    release = None

    async def stubborn():
        assert release is not None
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        result = await force_task_terminal(task, timeout=0.01)
        release.set()
        await task
        return task, result

    task, result = asyncio.run(scenario())

    assert task.done() is True
    assert result.terminal is False
    assert any(isinstance(error, TimeoutError) for error in result.errors)


def test_force_task_terminal_runs_cooperative_finally():
    finalized: list[bool] = []

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalized.append(True)

    async def scenario():
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        result = await force_task_terminal(task, timeout=0.1)
        return result

    result = asyncio.run(scenario())

    assert result.terminal is True
    assert result.errors == ()
    assert finalized == [True]


def test_cancel_tasks_and_wait_returns_only_cancellation_resistant_tasks():
    async def scenario():
        release = asyncio.Event()

        async def cooperative():
            await asyncio.Event().wait()

        async def resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        cooperative_task = asyncio.create_task(cooperative())
        resistant_task = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        pending = await cancel_tasks_and_wait(
            (cooperative_task, resistant_task, resistant_task),
            timeout=0.01,
        )
        assert pending == {resistant_task}
        release.set()
        await resistant_task

    asyncio.run(scenario())


def test_bounded_shutdown_runs_pending_task_finalizers():
    finalized: list[bool] = []

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalized.append(True)

    async def main():
        asyncio.create_task(background())
        await asyncio.sleep(0)
        return "done"

    assert run_with_bounded_shutdown(main(), shutdown_timeout=0.1) == "done"
    assert finalized == [True]


def test_bounded_shutdown_preserves_result_despite_cancellation_resistant_task():
    # A background task that refuses cancellation must NOT discard the
    # completed run's result: the run returns normally and the lingering task
    # is surfaced as a non-fatal diagnostic rather than a crash-on-exit.
    script = r'''
import asyncio
from opencollab.application.async_timeout import run_with_bounded_shutdown

async def stubborn():
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue

async def main():
    asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    return "RESULT_OK"

value = run_with_bounded_shutdown(main(), shutdown_timeout=0.01)
print("RESULT:" + repr(value))
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=1,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RESULT:'RESULT_OK'" in completed.stdout
    assert "missed the shutdown deadline" in completed.stderr


def test_bounded_shutdown_cancels_task_spawned_during_cleanup():
    child_cancelled: list[bool] = []

    async def child():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.append(True)
            raise

    async def parent():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            asyncio.create_task(child())

    async def main():
        asyncio.create_task(parent())
        await asyncio.sleep(0)
        return "done"

    assert run_with_bounded_shutdown(main(), shutdown_timeout=0.1) == "done"
    assert child_cancelled == [True]
