from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.tools.base import host_write_locks
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.tools import builtin_tools
from tests.support.docker_native_edits_support import LocalDockerTransport
from tests.workflows.test_candidate_adoption_recovery import _flatten, _repository, _run
from tests.workflows.test_candidate_shared_mutations import _WriterModel
from tests.workflows.test_workflow_candidate_workspace import _git


class _RemoteNamespaceTransport(LocalDockerTransport):
    remote = "/oc-native-alias-workspace"

    async def __call__(self, *argv, **kwargs):
        actual = str(self.workspace)
        mapped = tuple(arg.replace(self.remote, actual) if isinstance(arg, str) else arg for arg in argv)
        result = await super().__call__(*mapped, **kwargs)
        return ProcessResult(result.returncode, result.stdout.replace(actual.encode(), self.remote.encode()),
                             result.stderr.replace(actual.encode(), self.remote.encode()))


class _RemoteCandidates(EnvCandidateWorkspace):
    def __init__(self, environment, transport):
        super().__init__(environment)
        self.transport = transport

    async def _candidate_environment(self, path):
        environment = await super()._candidate_environment(path)
        self.transport.attach(environment)
        return environment


def _remote_environment(repo):
    transport = _RemoteNamespaceTransport(repo, pair_reads=False)
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
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment._temporary_files
        assert not environment.revoked
    finally:
        release.set()
        await environment.cleanup()


async def test_remote_alias_group_prepares_locks_before_acquisition_and_releases_cancelled_waiter(
    tmp_path, monkeypatch,
):
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings/value.py").write_text("VALUE = 1\n")
    (tmp_path / "alias").symlink_to("settings", target_is_directory=True)
    environment, _ = _remote_environment(tmp_path)
    from opencollab.adapters import _env_docker as docker_module
    resolve = environment._resolve_write_target
    resolving_task = asyncio.current_task()
    resolutions = []

    async def resolve_before_locks(target, **kwargs):
        assert not any(owner is resolving_task for owner in docker_module._WRITE_LOCK_OWNERS.values())
        result = await resolve(target, **kwargs)
        resolutions.append(result)
        return result

    monkeypatch.setattr(environment, "_resolve_write_target", resolve_before_locks)
    waiting = None
    try:
        async with host_write_locks((environment.workspace + "/alias/value.py",), environment):
            owned = [key.split("\0", 1)[1] for key, owner in docker_module._WRITE_LOCK_OWNERS.items()
                     if owner is resolving_task]
            assert owned == sorted(owned)
            assert owned == [environment.workspace + suffix for suffix in (
                "/alias", "/settings", "/settings/value.py",
            )]
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
        assert resolutions
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
