"""Native edit locks use the actual container execution directory."""

from __future__ import annotations

import asyncio
import shlex

import pytest

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.workflow_runtime import WorkflowSessionFactory
from tests.support.docker_native_edits_support import (
    LocalDockerTransport,
    docker_environment,
    edit_call,
    workflow_edits,
)


@pytest.mark.parametrize("relative", [(True, False), (False, True)])
@pytest.mark.parametrize("modes", [
    ("str_replace", "str_replace"), ("line_replace", "line_replace"), ("str_replace", "line_replace"),
])
async def test_default_workflow_mixed_paths_keep_both_edits(tmp_path, monkeypatch, relative, modes):
    result = await asyncio.wait_for(workflow_edits(tmp_path, monkeypatch, modes, relative=relative), 5)

    assert result["outputs"] == ["edit complete", "edit complete"]
    assert result["errors"] == []
    assert result["both_edits_retained"], result

@pytest.mark.parametrize("execution", ["container-default", "prefix-cd"])
async def test_relative_lock_matches_absolute_path_after_directory_resolution(tmp_path, execution):
    directory = tmp_path / "actual directory"
    directory.mkdir()
    target = directory / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    transport = LocalDockerTransport(directory)
    absolute = docker_environment(directory, transport)
    # workspace describes the view; the daemon or command prefix sets its cwd.
    relative = DockerEnvironment(
        workspace=str(tmp_path), container_id=absolute.container_reference,
        exec_workdir=str(tmp_path) if execution == "prefix-cd" else None,
        command_prefix=f"cd {shlex.quote(str(directory))}" if execution == "prefix-cd" else None,
    )
    transport.attach(relative)

    async def edit(index, environment, path):
        tool, params = edit_call("str_replace" if index == 0 else "line_replace", path, index)
        return await tool.execute_with_runtime(params, ToolRuntime(environment, None, None))

    outputs = await asyncio.wait_for(asyncio.gather(
        edit(0, relative, "module.py"), edit(1, absolute, str(target)),
    ), 5)

    assert all(not output.startswith("Error") for output in outputs), outputs
    assert target.read_text() == "alpha = 2\nbeta = 2\n"

async def test_relative_names_in_different_directories_remain_parallel(tmp_path):
    first_dir, second_dir = tmp_path / "one", tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    for directory in (first_dir, second_dir):
        (directory / "module.py").write_text("alpha = 1\nbeta = 1\n")
    class ReadConcurrencyTransport(LocalDockerTransport):
        active_reads = 0
        max_reads = 0

        async def __call__(self, *argv, timeout=120, input_bytes=None):
            reading = input_bytes is None and "cat -- " in argv[-1]
            if reading:
                self.active_reads += 1
                self.max_reads = max(self.max_reads, self.active_reads)
            try:
                return await super().__call__(*argv, timeout=timeout, input_bytes=input_bytes)
            finally:
                if reading:
                    self.active_reads -= 1

    transport = ReadConcurrencyTransport(tmp_path)
    first = docker_environment(first_dir, transport)
    second = docker_environment(second_dir, transport, container_id=first.container_reference)

    async def edit(index, environment):
        tool, params = edit_call("str_replace", "module.py", index)
        return await tool.execute_with_runtime(params, ToolRuntime(environment, None, None))

    outputs = await asyncio.wait_for(asyncio.gather(edit(0, first), edit(1, second)), 5)

    assert all(not output.startswith("Error") for output in outputs)
    assert transport.max_reads == 2
    assert (first_dir / "module.py").read_text() == "alpha = 2\nbeta = 1\n"
    assert (second_dir / "module.py").read_text() == "alpha = 1\nbeta = 2\n"

@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("parent_traversal", [False, True])
@pytest.mark.parametrize("modes", [
    ("str_replace", "str_replace"), ("line_replace", "line_replace"), ("str_replace", "line_replace"),
])
async def test_directory_aliases_share_native_edit_lock(tmp_path, relative, parent_traversal, modes):
    directory = tmp_path / "real"
    directory.mkdir()
    target = directory / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    alias = tmp_path / "linked"
    child = directory / "child"
    child.mkdir()
    alias.symlink_to(child if parent_traversal else directory, target_is_directory=True)
    transport = LocalDockerTransport(tmp_path)
    first = docker_environment(tmp_path, transport)
    second = docker_environment(tmp_path, transport, container_id=first.container_reference)
    alias_path = "linked/../module.py" if parent_traversal else "linked/module.py"
    paths = ("real/module.py", alias_path) if relative else (str(target), str(tmp_path / alias_path))

    async def edit(index, environment):
        tool, params = edit_call(modes[index], paths[index], index)
        return await tool.execute_with_runtime(params, ToolRuntime(environment, None, None))

    outputs = await asyncio.wait_for(asyncio.gather(edit(0, first), edit(1, second)), 5)

    assert all(not output.startswith("Error") for output in outputs), outputs
    assert target.read_text() == "alpha = 2\nbeta = 2\n"
    assert not first._active_execs and not second._active_execs

@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("mode", ["create", "str_replace", "line_replace", "unified_diff"])
async def test_symlink_parent_traversal_keeps_native_read_and_edit_target(tmp_path, relative, mode):
    child = tmp_path / "actual" / "child"
    child.mkdir(parents=True)
    (tmp_path / "alias").symlink_to(child, target_is_directory=True)
    target = child.parent / "module.py"
    target.write_text("alpha = 1\nbeta = 1\n")
    target.chmod(0o755)
    unrelated = tmp_path / "module.py"
    unrelated.write_text("unrelated = 7\n")
    unrelated.chmod(0o640)
    path = "alias/../module.py"
    if not relative:
        path = str(tmp_path / path)
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)
    displayed = await FileReadTool().execute_with_runtime({"path": path}, ToolRuntime(env, None, None))
    assert "1\talpha = 1" in displayed
    factory = WorkflowSessionFactory(
        model="offline", provider="openai", api_key=None, base_url=None, workspace=str(tmp_path), env=env,
    )
    if mode == "create":
        tool, params = FileWriteTool(), {"path": path, "mode": mode, "content": "alpha = 2\nbeta = 1\n"}
    else:
        tool, params = edit_call(mode, path, 0)

    output = await factory.execute_verification(tool, params)

    assert not output.startswith("Error"), output
    assert target.read_text() == "alpha = 2\nbeta = 1\n"
    assert unrelated.read_text() == "unrelated = 7\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert unrelated.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.rglob("*.opencollab-write-*.tmp"))
    assert not env._active_execs

async def test_parent_alias_resolution_keeps_final_symlink_replacement(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    referent = directory / "referent.txt"
    referent.write_text("original")
    target = directory / "module.txt"
    target.symlink_to(referent)
    alias = tmp_path / "linked"
    alias.symlink_to(directory, target_is_directory=True)
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)

    await env.write_file(str(alias / target.name), "replacement")

    assert not target.is_symlink()
    assert target.read_text() == "replacement"
    assert referent.read_text() == "original"

async def test_new_parent_directories_resolve_through_existing_alias(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "linked"
    alias.symlink_to(directory, target_is_directory=True)
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)

    await env.write_file("linked/new/nested/file.txt", "created")

    assert (directory / "new/nested/file.txt").read_text() == "created"

@pytest.mark.parametrize("path", ["config/default.toml", "missing/../module.py", "config/../module.py"])
async def test_docker_edit_requires_a_directory_before_parent_traversal_or_file_creation(tmp_path, path):
    (tmp_path / "config").write_text("config")
    (tmp_path / "module.py").write_text("original")
    environment = docker_environment(tmp_path, LocalDockerTransport(tmp_path, pair_reads=False))
    try:
        with pytest.raises(OSError, match="execution directory"):
            await FileWriteTool().execute_with_runtime({
                "path": path, "mode": "create", "content": "changed",
            }, ToolRuntime(environment, None, None))
        assert (tmp_path / "config").read_text() == "config"
        assert (tmp_path / "module.py").read_text() == "original"
    finally:
        await environment.cleanup()

@pytest.mark.parametrize("result", [
    ExecResult(1, "", "directory unavailable"),
    ExecResult(0, "/unframed/path\n", ""),
    ExecResult(0, "\0relative/path\n\0", ""),
])
async def test_unresolved_directory_does_not_start_a_relative_write(tmp_path, result, monkeypatch):
    target = tmp_path / "module.py"
    target.write_text("original\n")
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    env = docker_environment(tmp_path, transport)

    async def fail_directory(*_args, **_kwargs):
        return result

    monkeypatch.setattr(env, "_exec", fail_directory)
    with pytest.raises(OSError, match="execution directory"):
        await env.write_file("module.py", "changed\n")
    assert target.read_text() == "original\n"
