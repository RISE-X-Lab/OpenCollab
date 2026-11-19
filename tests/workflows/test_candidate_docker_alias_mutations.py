from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._candidate_increment import _apply_increment
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.tools.base import host_write_locks
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.tools import builtin_tools
from tests.support.docker_native_edits_support import LocalDockerTransport, RemoteDockerTransport, docker_environment
from tests.workflows.test_candidate_adoption_recovery import _flatten, _repository, _run
from tests.workflows.test_candidate_shared_mutations import _assert_flatten_recovery, _WriterModel
from tests.workflows.test_workflow_candidate_workspace import _git


class _RemoteCandidates(EnvCandidateWorkspace):
    def __init__(self, environment, transport):
        super().__init__(environment)
        self.transport = transport

    async def _candidate_environment(self, path):
        environment = await super()._candidate_environment(path)
        self.transport.attach(environment)
        return environment


def _remote_environment(repo):
    transport = RemoteDockerTransport(repo)
    environment = DockerEnvironment(
        workspace=transport.remote, container_id="a" * 64, exec_workdir=transport.remote,
        command_prefix="export OC_NATIVE_EDIT_PROBE=wrapped",
    )
    transport.attach(environment)
    return environment, transport


@pytest.mark.parametrize("tool", ["file_write", "apply_patch"])
@pytest.mark.parametrize("path", ["alias/runtime.cache", "settings/runtime.cache", "unrelated.cache"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_sdk_remote_alias_mutations_survive_directory_recovery(tmp_path, monkeypatch, tool, path, cancel):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\n*.cache\n")
    (repo / "alias").symlink_to("settings", target_is_directory=True)
    _git(repo, "add", ".gitignore", "alias")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: retain workspace directory alias")
    for cached in ("settings/runtime.cache", "unrelated.cache"):
        (repo / cached).write_text("VALUE = 1\n")
    loader = (repo / "loader.py").read_bytes()
    index = (repo / ".git/index").read_bytes()
    environment, transport = _remote_environment(repo)
    entered, release = asyncio.Event(), asyncio.Event()
    execute = environment.exec_cmd
    publications = []

    async def publication_barrier(cmd, timeout=120, **kwargs):
        publication = " apply --binary " in cmd and ".candidate-adopt-" in cmd
        if publication:
            entered.set()
            await release.wait()
        result = await execute(cmd, timeout, **kwargs)
        if publication:
            publications.append(result.returncode)
        return result

    monkeypatch.setattr(environment, "exec_cmd", publication_barrier)
    model = _WriterModel(tool, path)

    async def edit(child, _):
        _flatten(Path(child.workspace_root))
        return "prepared"

    async def flow(ctx, _):
        candidate = await ctx.candidate_workflow(edit, {}, label="flatten-directory")
        publication = asyncio.create_task(ctx.adopt_candidate(candidate))
        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            writer = asyncio.create_task(ctx.agent_run(
                "Set the runtime cache value to 100", tools=list(builtin_tools(tool, headless=False)),
                isolation="shared", budget=2000, system_prompt="Use the requested file tool",
            ))
            await asyncio.wait_for(model.requested.wait(), 10)
            if path == "unrelated.cache":
                assert (await asyncio.wait_for(asyncio.shield(writer), 10)).status == "completed"
            else:
                await asyncio.sleep(0.1)
                assert not writer.done()
                assert (repo / path).read_text() == "VALUE = 1\n"
            if cancel:
                publication.cancel()
                await asyncio.sleep(0)
                publication.cancel()
                assert not publication.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await publication
            else:
                with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
                    await publication
            assert (await asyncio.wait_for(writer, 10)).status == "completed"
            assert (repo / path).read_text() == "VALUE = 100\n"
            assert len(publications) == 1 and publications[0] != 0
            return "retained"
        finally:
            release.set()
            await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)

    try:
        result = await OpenCollab(repo, environment=environment, model="local-scripted").workflow(
            flow, llm=model, candidate_workspace=_RemoteCandidates(environment, transport), trace=False,
            timeout=60, budget=10000, concurrency=2, limit_mode="explicit", agent_profile="single2",
        )
        assert result.ok, result.reason
        assert result.output == "retained"
        assert _run(repo) == "1\n"
        assert (repo / ".git/index").read_bytes() == index
        _assert_flatten_recovery(repo, loader)
        assert not environment._temporary_files
        assert not environment.revoked
    finally:
        release.set()
        await environment.cleanup()


async def test_remote_alias_group_resolves_before_and_after_ordered_acquisition_and_releases_cancelled_waiter(
    tmp_path, monkeypatch,
):
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings/value.py").write_text("VALUE = 1\n")
    (tmp_path / "alias").symlink_to("settings", target_is_directory=True)
    environment, _ = _remote_environment(tmp_path)
    resolve = environment._resolve_write_target
    resolving_task = asyncio.current_task()
    resolutions = []
    expected = [environment.workspace + suffix for suffix in ("/alias", "/settings", "/settings/value.py")]

    async def resolve_with_lock_check(target, **kwargs):
        owned = [key.split("\0", 1)[1] for key, owner in docker_module._WRITE_LOCK_OWNERS.items()
                 if owner is resolving_task]
        assert owned in ([], expected)
        resolutions.append(bool(owned))
        return await resolve(target, **kwargs)

    monkeypatch.setattr(environment, "_resolve_write_target", resolve_with_lock_check)
    waiting = None
    try:
        async with host_write_locks((environment.workspace + "/alias/value.py",), environment):
            owned = [key.split("\0", 1)[1] for key, owner in docker_module._WRITE_LOCK_OWNERS.items()
                     if owner is resolving_task]
            assert owned == expected
        assert resolutions == sorted(resolutions)
        assert set(resolutions) == {False, True}
        monkeypatch.setattr(environment, "_resolve_write_target", resolve)
        async with host_write_locks((environment.workspace + "/settings",), environment):
            waiting = asyncio.create_task(FileWriteTool().execute_with_runtime({
                "path": "alias/value.py", "mode": "create", "content": "VALUE = 100\n",
            }, ToolRuntime(environment, None, None)))
            await asyncio.sleep(0.1)
            assert not waiting.done()
            alias_key = environment._container_id + "\0" + environment.workspace + "/alias"
            assert alias_key in docker_module._WRITE_LOCK_OWNERS
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert alias_key not in docker_module._WRITE_LOCK_OWNERS
        output = await asyncio.wait_for(FileWriteTool().execute_with_runtime({
            "path": "alias/value.py", "mode": "create", "content": "VALUE = 100\n",
        }, ToolRuntime(environment, None, None)), 5)
        assert "Created/wrote" in output
        assert (tmp_path / "settings/value.py").read_text() == "VALUE = 100\n"
        assert not docker_module._WRITE_LOCK_OWNERS
    finally:
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        await environment.cleanup()


async def test_docker_edits_serialize_on_the_target_after_candidate_retargets_directory_alias(
    tmp_path, monkeypatch,
):
    repo = _repository(tmp_path)
    for directory in ("old", "new"):
        (repo / directory).mkdir()
        (repo / directory / "module.py").write_text("alpha = 1\nbeta = 1\n")
    alias = repo / "alias"
    alias.symlink_to("old", target_is_directory=True)
    _git(repo, "add", "old", "new", "alias")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: keep two module directories")
    alias.unlink()
    alias.symlink_to("new", target_is_directory=True)
    patch = _git(repo, "diff", "--binary", "--", "alias")
    _git(repo, "restore", "--", "alias")

    environment = docker_environment(repo, LocalDockerTransport(repo))
    runtime = ToolRuntime(environment, None, None)
    entered, release = asyncio.Event(), asyncio.Event()
    execute, read = environment.exec_cmd, environment.read_file

    async def pause_publication(cmd, *args, **kwargs):
        if " apply --binary " in cmd and ".candidate-adopt-" in cmd:
            entered.set()
            await release.wait()
        return await execute(cmd, *args, **kwargs)

    async def read_under_target_lock(path):
        target_key = environment.container_reference + "\0" + str(repo / "new/module.py")
        assert docker_module._WRITE_LOCK_OWNERS.get(target_key) is asyncio.current_task()
        return await read(path)

    async def edit(path, name):
        return await FileWriteTool().execute_with_runtime({
            "path": path, "mode": "str_replace", "old_str": f"{name} = 1", "new_str": f"{name} = 2",
        }, runtime)

    async def wait_for_alias_editor():
        alias_key = environment.container_reference + "\0" + str(alias)
        while getattr(docker_module._WRITE_LOCKS.get(alias_key), "users", 0) < 2:
            await asyncio.sleep(0.005)

    monkeypatch.setattr(environment, "exec_cmd", pause_publication)
    monkeypatch.setattr(environment, "read_file", read_under_target_lock)
    publication = asyncio.create_task(_apply_increment(environment, str(repo), patch))
    writers = []
    try:
        await asyncio.wait_for(entered.wait(), 5)
        writers.append(asyncio.create_task(edit("alias/module.py", "alpha")))
        await asyncio.wait_for(wait_for_alias_editor(), 5)
        assert not writers[0].done()
        release.set()
        await asyncio.wait_for(publication, 5)
        writers.append(asyncio.create_task(edit("new/module.py", "beta")))
        receipts = await asyncio.wait_for(asyncio.gather(*writers), 5)
        assert all(receipt.startswith("Replaced") for receipt in receipts), receipts
        assert (repo / "old/module.py").read_text() == "alpha = 1\nbeta = 1\n"
        assert (repo / "new/module.py").read_text() == "alpha = 2\nbeta = 2\n"
    finally:
        release.set()
        await asyncio.gather(publication, *writers, return_exceptions=True)
        await environment.cleanup()


@pytest.mark.parametrize("new_file", ["default.toml", "nested/default.toml"])
@pytest.mark.parametrize("symlink", [False, True])
async def test_sdk_docker_candidate_can_replace_a_file_with_a_directory(tmp_path, new_file, symlink):
    repo = _repository(tmp_path)
    if symlink:
        (repo / "config-source").write_text("enabled = true\n")
        (repo / "config").symlink_to("config-source")
        _git(repo, "add", "config", "config-source")
    else:
        (repo / "config").write_text("enabled = true\n")
        _git(repo, "add", "config")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: keep a flat config")
    index = (repo / ".git/index").read_bytes()
    environment, transport = _remote_environment(repo)

    async def edit(child, _):
        config = Path(child.workspace_root) / "config"
        config.unlink()
        target = config / new_file
        target.parent.mkdir(parents=True)
        target.write_text("enabled = false\n")

    async def flow(ctx, _):
        candidate = await ctx.candidate_workflow(edit, {}, label="nested-config")
        await ctx.adopt_candidate(candidate)
        return "adopted"

    try:
        result = await OpenCollab(repo, environment=environment).workflow(
            flow, candidate_workspace=_RemoteCandidates(environment, transport), trace=False, timeout=30,
        )
        assert result.ok, result.reason
        assert result.output == "adopted"
        assert (repo / "config" / new_file).read_text() == "enabled = false\n"
        if symlink:
            assert (repo / "config-source").read_text() == "enabled = true\n"
        assert (repo / ".git/index").read_bytes() == index
    finally:
        await environment.cleanup()


async def test_sdk_docker_candidate_uses_the_same_git_root_for_capture_and_adoption(tmp_path):
    repo = _repository(tmp_path)
    package = repo / "package"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n")
    _git(repo, "add", "package")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare a subdirectory workflow")
    index = (repo / ".git/index").read_bytes()
    transport = LocalDockerTransport(repo, pair_reads=False)
    environment = docker_environment(package, transport)

    async def edit(child, _):
        (Path(child.workspace_root).parent / "other.py").write_text("VALUE = 2\n")

    async def flow(ctx, _):
        candidate = await ctx.candidate_workflow(edit, {}, label="update-neighbor")
        assert "b/other.py" in candidate.effective_diff
        await ctx.adopt_candidate(candidate)
        return "adopted"

    try:
        result = await OpenCollab(package, environment=environment).workflow(
            flow, candidate_workspace=_RemoteCandidates(environment, transport), trace=False, timeout=30,
        )
        assert result.ok, result.reason
        assert (repo / "other.py").read_text() == "VALUE = 2\n"
        assert (repo / ".git/index").read_bytes() == index
        assert environment._resolved_workspace in {None, str(package)}
    finally:
        await environment.cleanup()


async def test_docker_group_reuses_sibling_parent_resolution_in_each_pass(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    transport = LocalDockerTransport(tmp_path, pair_reads=False)
    environment = docker_environment(tmp_path, transport)
    try:
        paths = tuple(str(source / f"module_{index}.py") for index in range(48))
        async with environment.file_write_locks(paths):
            resolutions = [row for row in transport.calls if "opencollab-file-parent" in row["command"]]
            assert len(resolutions) == 4
            await environment.write_file(paths[0], "VALUE = 1\n")
        assert (source / "module_0.py").read_text() == "VALUE = 1\n"
    finally:
        await environment.cleanup()
