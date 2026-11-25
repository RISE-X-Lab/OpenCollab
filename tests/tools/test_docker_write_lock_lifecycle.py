from __future__ import annotations

import asyncio
import errno
import stat
from contextlib import AsyncExitStack

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from tests.support.docker_native_edits_support import (
    LocalDockerTransport,
    docker_environment,
    edit_call,
)


def test_attached_container_edits_work_across_repeated_event_loops(tmp_path):
    target = tmp_path / "module.py"
    container_id = "a" * 64

    async def edit_round():
        target.write_text("alpha = 1\nbeta = 1\n")
        transport = LocalDockerTransport(tmp_path)
        environment = docker_environment(tmp_path, transport, container_id=container_id)

        async def edit(index):
            tool, params = edit_call("str_replace", str(target), index)
            return await tool.execute_with_runtime(params, ToolRuntime(environment, None, None))

        try:
            outputs = await asyncio.gather(edit(0), edit(1))
            assert all(not output.startswith("Error") for output in outputs)
            assert target.read_text() == "alpha = 2\nbeta = 2\n"
        finally:
            await environment.cleanup()

    for _ in range(2):
        asyncio.run(edit_round())
    assert docker_module._WRITE_LOCKS == {}
    assert docker_module._WRITE_LOCK_OWNERS == {}


@pytest.mark.parametrize(("outer", "inner"), [("src/data.txt", "src"), ("src", "src/data.txt")])
async def test_docker_native_edit_can_nest_ancestor_and_child_locks(tmp_path, outer, inner):
    directory = tmp_path / "src"
    directory.mkdir(mode=0o755)
    target = directory / "data.txt"
    target.write_text("content 1\n")
    environment = docker_environment(tmp_path, LocalDockerTransport(tmp_path, pair_reads=False))

    async def edit():
        async with environment.file_write_lock(outer):
            async with environment.file_write_lock(inner):
                result = await environment.exec_cmd("chmod 0750 src")
                assert result.returncode == 0, result
                return await FileWriteTool().execute_with_runtime(
                    {"mode": "str_replace", "path": "src/data.txt", "old_str": "content 1", "new_str": "content 2"},
                    ToolRuntime(environment, None, None),
                )

    try:
        output = await asyncio.wait_for(edit(), 5)
        assert output.startswith("Replaced"), output
        assert target.read_text() == "content 2\n"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750
        assert docker_module._WRITE_LOCKS == {}
        assert docker_module._WRITE_LOCK_OWNERS == {}
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("cancel", [False, True])
async def test_docker_ancestor_upgrade_preserves_other_readers_and_its_outer_share(tmp_path, cancel):
    environment = docker_environment(tmp_path, LocalDockerTransport(tmp_path))
    lock = environment._resolved_file_write_lock
    target = str(tmp_path / "src")
    key = environment.container_reference + "\0" + target
    upgrading, exclusive, shared_again = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_exclusive, release_shared, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = []

    async def upgrade():
        async with lock(target, shared=True):
            upgrading.set()
            try:
                async with lock(target):
                    exclusive.set()
                    await release_exclusive.wait()
            except asyncio.CancelledError:
                if not cancel:
                    raise
            shared_again.set()
            await release_shared.wait()

    async def write():
        writing.set()
        async with lock(target):
            return "wrote"

    try:
        async with lock(target, shared=True):
            upgrade_task = asyncio.create_task(upgrade())
            tasks.append(upgrade_task)
            await asyncio.wait_for(upgrading.wait(), 5)
            assert not exclusive.is_set()
            if cancel:
                upgrade_task.cancel()
                await asyncio.wait_for(shared_again.wait(), 5)
                assert upgrade_task not in docker_module._WRITE_LOCK_WAITS
        if not cancel:
            await asyncio.wait_for(exclusive.wait(), 5)
        writer = asyncio.create_task(write())
        tasks.append(writer)
        await asyncio.wait_for(writing.wait(), 5)
        assert not writer.done()
        release_exclusive.set()
        await asyncio.wait_for(shared_again.wait(), 5)
        assert docker_module._WRITE_LOCKS[key].shared_owners == {upgrade_task}
        assert key not in docker_module._WRITE_LOCK_OWNERS
        assert not writer.done()
        release_shared.set()
        assert await asyncio.wait_for(writer, 5) == "wrote"
        await asyncio.wait_for(upgrade_task, 5)
        assert docker_module._WRITE_LOCKS == {}
        assert docker_module._WRITE_LOCK_OWNERS == {}
        assert docker_module._WRITE_LOCK_WAITS == {}
    finally:
        release_shared.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await environment.cleanup()


@pytest.mark.parametrize("different_targets", [False, True])
async def test_docker_competing_upgrades_report_deadlock_and_release_the_waiter(tmp_path, different_targets):
    environment = docker_environment(tmp_path, LocalDockerTransport(tmp_path))
    lock = environment._resolved_file_write_lock
    first = str(tmp_path / "first")
    second = str(tmp_path / "second") if different_targets else first
    targets = sorted({first, second})
    both_shared, first_attempt, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def upgrade_first():
        async with AsyncExitStack() as stack:
            for target in targets:
                await stack.enter_async_context(lock(target, shared=True))
            await both_shared.wait()
            first_attempt.set()
            async with lock(first):
                assert second_ready.is_set()
                return "promoted"

    async def upgrade_second():
        async with AsyncExitStack() as stack:
            for target in targets:
                await stack.enter_async_context(lock(target, shared=True))
            both_shared.set()
            await first_attempt.wait()
            try:
                async with lock(second):
                    pytest.fail("promotion bypassed the other shared owner")
            finally:
                second_ready.set()

    try:
        result, error = await asyncio.wait_for(
            asyncio.gather(upgrade_first(), upgrade_second(), return_exceptions=True), 5,
        )
        assert result == "promoted"
        assert isinstance(error, OSError) and error.errno == errno.EDEADLK
        assert docker_module._WRITE_LOCKS == {}
        assert docker_module._WRITE_LOCK_OWNERS == {}
        assert docker_module._WRITE_LOCK_WAITS == {}
    finally:
        await environment.cleanup()
