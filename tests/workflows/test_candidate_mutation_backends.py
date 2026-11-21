from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters._candidate_increment import _apply_increment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.adapters.tools.base import host_write_locks
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.authored_workflow_bridge import AuthoredWorkflowBridge, BridgeClient, BridgeEnvironment
from tests.support.docker_native_edits_support import LocalDockerTransport, docker_environment
from tests.workflows.test_candidate_adoption_recovery import _detached_files, _flatten, _repository, _run
from tests.workflows.test_workflow_candidate_workspace import _git


@pytest.mark.parametrize("backend", ["local", "docker"])
@pytest.mark.parametrize("bridge_entry", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_candidate_and_bridge_share_native_mutation_ownership(
    tmp_path, monkeypatch, backend, bridge_entry, failure,
):
    monkeypatch.chdir(tmp_path)
    repo = _repository(tmp_path)
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare native publication")
    if failure:
        _flatten(repo)
    else:
        (repo / "settings/defaults.py").write_text("VALUE = 42\n")
    _git(repo, "add", "-A")
    patch = _git(repo, "diff", "--cached", "--binary")
    _git(repo, "reset", "--hard", "-q")
    if failure:
        assert _run(repo, cache=True) == "1\n"
    else:
        (repo / "settings/independent.py").write_text("before\n")
    index = (repo / ".git/index").read_bytes()
    native = (LocalEnvironment(str(repo)) if backend == "local" else
              docker_environment(repo, LocalDockerTransport(repo, pair_reads=False)))
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = native.exec_cmd

    async def publication_barrier(cmd, timeout=120, **kwargs):
        command = cmd
        if " apply --binary " in command and ".candidate-adopt-" in command:
            entered.set()
            await release.wait()
        return await execute(command, timeout, **kwargs)

    monkeypatch.setattr(native, "exec_cmd", publication_barrier)
    mailbox_root = tmp_path / "mailbox"
    artifacts = mailbox_root / "artifacts"
    artifacts.mkdir(parents=True)
    mailbox = LocalEnvironment(str(mailbox_root))
    bridge = AuthoredWorkflowBridge(
        mailbox, str(artifacts),
        SessionEnvironmentPool(str(mailbox_root), isolation="shared", base_environment=mailbox),
        {}, ToolRuntime(mailbox, None, None), allow_unisolated_shell=True,
    )
    bridge._environments[1] = native
    proxy = BridgeEnvironment(BridgeClient(artifacts, bridge.options()), {
        "handle": 1, "workspace": native.workspace, "process_isolated": False,
    })

    async def serve():
        while not stop.is_set():
            await bridge.poll()
            await asyncio.sleep(0.005)

    server = asyncio.create_task(serve())
    publication = asyncio.create_task(_apply_increment(proxy if bridge_entry else native, str(repo), patch))
    writer = None
    try:
        entered_task = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait((entered_task, publication), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if publication.done():
                await publication
            await asyncio.wait_for(entered_task, 1)
        finally:
            entered_task.cancel()
            await asyncio.gather(entered_task, return_exceptions=True)
        writer_runtime = ToolRuntime(native if bridge_entry else proxy, None, None)
        if not failure:
            result = await asyncio.wait_for(FileWriteTool().execute_with_runtime({
                "path": "settings/independent.py", "mode": "str_replace",
                "old_str": "before", "new_str": "after",
            }, writer_runtime), 5)
            assert "Replaced" in result
            assert (repo / "settings/independent.py").read_text() == "after\n"
        writer = asyncio.create_task(FileWriteTool().execute_with_runtime({
            "path": "settings/defaults.py", "mode": "create", "content": "VALUE = 100\n",
        }, writer_runtime))
        await asyncio.sleep(0.05)
        assert not writer.done()
        assert (repo / "settings/defaults.py").read_text() == "VALUE = 1\n"
        release.set()
        if failure:
            with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
                await asyncio.wait_for(publication, 10)
        else:
            await asyncio.wait_for(publication, 10)
        assert "Created/wrote" in await asyncio.wait_for(writer, 10)
        assert _run(repo) == "100\n"
        assert (repo / ".git/index").read_bytes() == index
        assert not native.revoked
        assert not bridge._edits
    finally:
        release.set()
        await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)
        stop.set()
        await server
        await bridge.aclose()
        await native.cleanup()
        await mailbox.cleanup()


async def test_cancelled_publication_recovers_before_releasing_shared_writer(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    target = repo / "settings/defaults.py"
    sibling = repo / "settings/independent.py"
    sibling.write_text("before\n")
    index = (repo / ".git/index").read_bytes()
    target.write_text("VALUE = 42\n")
    patch = _git(repo, "diff", "--binary", "--", "settings/defaults.py")
    target.write_text("VALUE = 1\n")
    environment = LocalEnvironment(str(repo))
    entered = asyncio.Event()
    execute = environment.exec_cmd

    async def pause_publication(command, timeout=120, **kwargs):
        if " apply --binary " in command and ".candidate-adopt-" in command:
            # A real completed Git publication is cancelled before its caller
            # receives the result, exercising recovery of changed bytes.
            result = await execute(command, timeout, **kwargs)
            assert result.returncode == 0
            entered.set()
            await asyncio.Event().wait()
        return await execute(command, timeout, **kwargs)

    monkeypatch.setattr(environment, "exec_cmd", pause_publication)
    publication = asyncio.create_task(_apply_increment(environment, str(repo), patch))
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        runtime = ToolRuntime(environment, None, None)
        result = await asyncio.wait_for(FileWriteTool().execute_with_runtime({
            "path": str(sibling), "mode": "str_replace", "old_str": "before", "new_str": "during",
        }, runtime), 5)
        assert "Replaced" in result
        writer = asyncio.create_task(FileWriteTool().execute_with_runtime({
            "path": str(target), "mode": "create", "content": "VALUE = 100\n",
        }, runtime))
        await asyncio.sleep(0.05)
        assert not writer.done()
        publication.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publication
        assert "Created/wrote" in await asyncio.wait_for(writer, 10)
        assert _run(repo) == "100\n"
        assert sibling.read_text() == "during\n"
        assert (repo / ".git/index").read_bytes() == index
        assert _detached_files(repo / ".git") == (
            {target: b"VALUE = 1\n"}, {target: b"VALUE = 42\n"},
        )
    finally:
        if not publication.done():
            publication.cancel()
        await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)
        await environment.cleanup()


@pytest.mark.parametrize("backend", ["local", "docker"])
@pytest.mark.parametrize("attribute_source", ["nested", "root-missing", "configured"])
async def test_in_place_publication_keeps_git_attributes_stable_through_recovery(
    tmp_path, monkeypatch, backend, attribute_source,
):
    repo = _repository(tmp_path)
    _git(repo, "config", "core.autocrlf", "false")
    target = repo / "settings/value.txt"
    target.write_bytes(b"original\n")
    attributes = {
        "nested": repo / "settings/.gitattributes",
        "root-missing": repo / ".gitattributes",
        "configured": repo / "line-endings",
    }[attribute_source]
    if attribute_source != "root-missing":
        attributes.write_text("*.txt text eol=lf\n")
    if attribute_source == "configured":
        _git(repo, "config", "core.attributesFile", attributes.name)
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare in-place publication")
    target.write_bytes(b"candidate\n")
    patch = _git(repo, "diff", "--binary", "--", "settings/value.txt")
    target.write_bytes(b"original\n")
    index = (repo / ".git/index").read_bytes()

    environment = (LocalEnvironment(str(repo)) if backend == "local" else
                   docker_environment(repo, LocalDockerTransport(repo, pair_reads=False)))
    entered, publish = asyncio.Event(), asyncio.Event()
    recovering, recover = asyncio.Event(), asyncio.Event()
    execute = environment.exec_cmd

    async def observe(cmd, timeout=120, **kwargs):
        if " apply --binary " in cmd and ".candidate-adopt-" in cmd:
            entered.set()
            await publish.wait()
            result = await execute(cmd, timeout, **kwargs)
            assert result.returncode == 0
            assert target.read_bytes() == b"candidate\n"
            raise RuntimeError("publication acknowledgement lost")
        if cmd.startswith("failed=0;"):
            recovering.set()
            await recover.wait()
        return await execute(cmd, timeout, **kwargs)

    monkeypatch.setattr(environment, "exec_cmd", observe)
    publication = asyncio.create_task(_apply_increment(environment, str(repo), patch))
    writer = None
    runtime = ToolRuntime(environment, None, None)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        writer = asyncio.create_task(FileWriteTool().execute_with_runtime({
            "path": str(attributes), "mode": "create", "overwrite": True, "content": "*.txt text eol=crlf\n",
        }, runtime))
        sibling = await asyncio.wait_for(FileWriteTool().execute_with_runtime({
            "path": "settings/independent.txt", "mode": "create", "content": "sibling edit\n",
        }, runtime), 5)
        assert "Created/wrote" in sibling
        assert not writer.done()
        publish.set()
        await asyncio.wait_for(recovering.wait(), 10)
        assert not writer.done()
        recover.set()
        with pytest.raises(RuntimeError, match="^publication acknowledgement lost") as failure:
            await asyncio.wait_for(publication, 10)
        assert type(failure.value) is RuntimeError
        assert "Created/wrote" in await asyncio.wait_for(writer, 10)
        assert target.read_bytes() == b"original\n"
        assert attributes.read_text() == "*.txt text eol=crlf\n"
        assert (repo / "settings/independent.txt").read_text() == "sibling edit\n"
        assert (repo / ".git/index").read_bytes() == index
        assert not environment.revoked
    finally:
        publish.set()
        recover.set()
        await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)
        await environment.cleanup()


async def test_local_mutation_lock_normalizes_workspace_parent_aliases(tmp_path):
    workspace = tmp_path / "workspace"
    target = workspace / "nested/value.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n")
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    environment = LocalEnvironment(str(workspace))
    writer = None
    try:
        async with host_write_locks((str(alias / "nested/value.py"),), environment):
            writer = asyncio.create_task(FileWriteTool().execute_with_runtime({
                "path": str(target), "mode": "create", "content": "VALUE = 100\n",
            }, ToolRuntime(environment, None, None)))
            await asyncio.sleep(0.05)
            assert not writer.done()
            assert target.read_text() == "VALUE = 1\n"
        assert "Created/wrote" in await asyncio.wait_for(writer, 2)
        assert target.read_text() == "VALUE = 100\n"
    finally:
        if writer is not None:
            await asyncio.gather(writer, return_exceptions=True)
        await environment.cleanup()
