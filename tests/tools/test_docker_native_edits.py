"""Shared-container native edits retain successful non-overlapping changes."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import host_write_lock
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from tests.support.docker_native_edits_support import (
    LocalDockerTransport,
    docker_environment,
    edit_call,
    workflow_edits,
)


@pytest.mark.parametrize("modes", [
    ("str_replace", "str_replace"),
    ("line_replace", "line_replace"),
    ("unified_diff", "unified_diff"),
    ("str_replace", "line_replace"),
    ("line_replace", "str_replace"),
    ("str_replace", "unified_diff"),
])
async def test_two_shared_container_sessions_retain_both_edits(tmp_path, monkeypatch, modes):
    result = await asyncio.wait_for(workflow_edits(tmp_path, monkeypatch, modes), 5)

    assert result["session_count"] == 2
    assert result["outputs"] == ["edit complete", "edit complete"]
    assert result["errors"] == []
    assert all(len(outputs) == 1 and not outputs[0].startswith("Error") for outputs in result["tool_outputs"])
    assert result["both_edits_retained"], result
    assert all(call["wrapped"] for call in result["calls"])
    assert all("OC_NATIVE_EDIT_PROBE=wrapped" in call["command"] for call in result["calls"])

@pytest.mark.parametrize(("backend", "parallel", "separate"), [
    ("docker", False, False), ("local", True, False), ("docker", True, True),
])
@pytest.mark.parametrize("modes", [("str_replace", "str_replace"), ("line_replace", "line_replace")])
async def test_native_edit_controls(tmp_path, monkeypatch, backend, parallel, separate, modes):
    result = await asyncio.wait_for(workflow_edits(
        tmp_path, monkeypatch, modes, backend=backend, parallel=parallel, separate=separate,
    ), 5)

    assert result["both_edits_retained"], result
    assert result["errors"] == []
    if separate:
        assert result["max_real_exec_tracking"] == 2

async def test_native_edit_of_sibling_file_completes_while_first_read_is_paused(tmp_path):
    directory = (tmp_path / "src").resolve()
    directory.mkdir()
    first_path, second_path = directory / "first.py", directory / "second.py"
    for path in (first_path, second_path):
        path.write_text("alpha = 1\nbeta = 1\n")
    paused, release = asyncio.Event(), asyncio.Event()

    class PausedReadTransport(LocalDockerTransport):
        async def __call__(self, *argv, **kwargs):
            result = await super().__call__(*argv, **kwargs)
            if "cat -- " in argv[-1] and str(first_path) in argv[-1]:
                paused.set()
                await release.wait()
            return result

    transport = PausedReadTransport(tmp_path, pair_reads=False)
    first = docker_environment(tmp_path, transport)
    second = docker_environment(tmp_path, transport, container_id=first.container_reference)

    async def edit(path, index, environment):
        tool, params = edit_call("str_replace", str(path), index)
        return await tool.execute_with_runtime(params, ToolRuntime(environment, None, None))

    first_edit = asyncio.create_task(edit(first_path, 0, first))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        receipt = await asyncio.wait_for(edit(second_path, 1, second), 5)
        assert receipt.startswith("Replaced")
        assert not first_edit.done()
        assert second_path.read_text() == "alpha = 1\nbeta = 2\n"
    finally:
        release.set()
        await asyncio.wait_for(first_edit, 5)
        await first.cleanup()
        await second.cleanup()
    assert first_path.read_text() == "alpha = 2\nbeta = 1\n"

async def test_two_container_views_share_the_normalized_target_lock(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(tmp_path)
    first = docker_environment(tmp_path, transport)
    second = docker_environment(tmp_path, transport, container_id=first.container_reference)
    paths = [str(target), str(tmp_path) + "/./module.py"]

    async def edit(index, env):
        tool, params = edit_call("str_replace" if index == 0 else "line_replace", paths[index], index)
        return await tool.execute_with_runtime(params, ToolRuntime(env, None, None))

    outputs = await asyncio.wait_for(asyncio.gather(edit(0, first), edit(1, second)), 5)

    assert all(not output.startswith("Error") for output in outputs)
    assert target.read_text() == "alpha = 2\nbeta = 2\n"

@pytest.mark.parametrize("second_mode", ["str_replace", "line_replace"])
async def test_conflicting_edits_recheck_current_content(tmp_path, second_mode):
    target = tmp_path / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(tmp_path)
    env = docker_environment(tmp_path, transport)
    runtime = ToolRuntime(env, None, None)
    first_tool, first = edit_call("str_replace", str(target), 0)
    second_tool, second = edit_call(second_mode, str(target), 0)
    second["new_str"] = "alpha = 3"

    outputs = await asyncio.wait_for(asyncio.gather(
        first_tool.execute_with_runtime(first, runtime), second_tool.execute_with_runtime(second, runtime),
    ), 5)

    assert sum(output.startswith("Error") for output in outputs) == 1
    assert target.read_text() in {"alpha = 2\nbeta = 1\n", "alpha = 3\nbeta = 1\n"}
    assert len([call for call in transport.calls if call["write"]]) == 1

async def test_unique_match_and_overwrite_checks_survive_container_lock(tmp_path):
    target = tmp_path / "module.py"
    original = "alpha = 1\nalpha = 1\n" + "# retained\n" * 100
    target.write_text(original)
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)
    runtime = ToolRuntime(env, None, None)

    duplicate = await FileWriteTool().execute_with_runtime({
        "path": str(target), "mode": "str_replace", "old_str": "alpha = 1", "new_str": "alpha = 2",
    }, runtime)
    short = await FileWriteTool().execute_with_runtime({
        "path": str(target), "mode": "create", "content": "short\n",
    }, runtime)
    stale = await ApplyPatchTool().execute_with_runtime({
        "path": str(target), "mode": "line_replace", "start_line": 1, "end_line": 1,
        "expected_str": "alpha = 0", "new_str": "alpha = 2",
    }, runtime)

    assert duplicate.startswith("Error")
    assert "much shorter" in short
    assert stale.startswith("Error")
    assert target.read_text() == original
    assert not any(call["write"] for call in transport.calls)

async def test_cancelled_lock_owner_releases_waiting_native_edit(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)
    acquired = asyncio.Event()

    async def own_lock():
        async with host_write_lock(str(target), env):
            acquired.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(own_lock())
    await acquired.wait()
    tool, params = edit_call("str_replace", str(target), 0)
    waiter = asyncio.create_task(tool.execute_with_runtime(params, ToolRuntime(env, None, None)))
    try:
        await asyncio.sleep(0.02)
        assert all("opencollab-file-parent" in call["command"] for call in transport.calls)
    finally:
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        output = await asyncio.wait_for(waiter, 5)

    assert not output.startswith("Error")
    assert target.read_text() == "alpha = 2\nbeta = 1\n"
    assert not env.revoked

async def test_child_task_waits_and_cancelled_waiter_keeps_owner_usable(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)
    runtime = ToolRuntime(env, None, None)
    tool, params = edit_call("str_replace", str(target), 0)

    async def own_and_cancel_waiter():
        async with host_write_lock(str(target), env):
            waiter = asyncio.create_task(tool.execute_with_runtime(params, runtime))
            try:
                async def wait_for_resolved_waiter():
                    while len(transport.calls) < 2 or env._active_execs:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(wait_for_resolved_waiter(), 2)
                assert all("opencollab-file-parent" in call["command"] for call in transport.calls)
            finally:
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            # The direct await preserves this task's ownership during reentry.
            output = await FileWriteTool().execute_with_runtime({
                "path": str(target), "mode": "create", "content": "alpha = 2\nbeta = 1\n", "overwrite": True,
            }, runtime)
            assert not output.startswith("Error")

    await asyncio.wait_for(own_and_cancel_waiter(), 5)

    tool, params = edit_call("line_replace", str(target), 1)
    output = await asyncio.wait_for(tool.execute_with_runtime(params, runtime), 5)
    assert not output.startswith("Error")
    assert target.read_text() == "alpha = 2\nbeta = 2\n"
    assert not env.revoked
