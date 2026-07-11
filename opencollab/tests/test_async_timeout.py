from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import opencollab.application.async_timeout as async_timeout_module
import pytest
from opencollab.application.async_timeout import (
    AsyncRuntimeUnhealthyError,
    abandon_on_timeout,
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
    async def stubborn():
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue

    async def scenario():
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        result = await force_task_terminal(task, timeout=0.01)
        return task, result

    task, result = asyncio.run(scenario())

    assert task.done() is False
    assert result.terminal is False
    assert any(isinstance(error, TimeoutError) for error in result.errors)


def test_detached_capacity_escalates_runtime_unhealthy(monkeypatch):
    release = None

    async def scenario():
        nonlocal release
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        monkeypatch.setattr(
            async_timeout_module,
            "_detach_task_from_loop",
            lambda _task: True,
        )
        with pytest.raises(AsyncRuntimeUnhealthyError, match="process restart"):
            await force_task_terminal(task, timeout=0.01)
        release.set()
        await task

    try:
        asyncio.run(scenario())
    finally:
        async_timeout_module._ASYNC_RUNTIME_UNHEALTHY = False


def test_force_task_terminal_isolates_synchronously_blocking_finally():
    script = r'''
import asyncio
import threading

from opencollab.application.async_timeout import force_task_terminal

blocked = threading.Event()

async def stubborn():
    try:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue
    finally:
        blocked.wait()

async def main():
    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    result = await force_task_terminal(task, timeout=0.02)
    assert result.terminal is False

asyncio.run(main())
'''
    package_root = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ)
    env["PYTHONPATH"] = package_root

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=2,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_bounded_shutdown_does_not_run_blocking_pending_finally():
    script = r'''
import asyncio
import threading

from opencollab.application.async_timeout import run_with_bounded_shutdown

blocked = threading.Event()

async def background():
    try:
        await asyncio.Event().wait()
    finally:
        blocked.wait()

async def main():
    asyncio.create_task(background())
    await asyncio.sleep(0)

run_with_bounded_shutdown(main(), shutdown_timeout=0.02)
'''
    package_root = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ)
    env["PYTHONPATH"] = package_root

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=2,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
