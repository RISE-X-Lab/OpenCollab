from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencollab.adapters import _docker_snapshot as snapshot_module
from opencollab.adapters._docker_snapshot import _copy_artifact_to_host
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters._env_vm import VMEnvironment, VMTransportError
from opencollab.adapters.env import DockerEnvironment, ExecResult, LocalEnvironment, WorktreeEnvironment
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.authored_workflow import AuthoredWorkflowTool, _Child
from opencollab.bootstrap.authored_workflow_bridge import AuthoredWorkflowBridge, BridgeClient, BridgeEnvironment
from tests.support.docker_native_edits_support import LocalDockerTransport, docker_environment


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("component", ["directory", "symlink", "missing"])
async def test_artifact_transfer_keeps_parent_traversal_identity(tmp_path, absolute, component):
    workspace = tmp_path / "workspace"
    (workspace / "actual/child").mkdir(parents=True)
    (workspace / "result.json").write_text('{"selected": 42}')
    (workspace / "actual/result.json").write_text('{"other": 7}')
    if component == "directory":
        (workspace / "parent").mkdir()
    elif component == "symlink":
        (workspace / "parent").symlink_to("actual/child", target_is_directory=True)
    relative = "parent/../result.json"
    source = os.path.join(str(workspace), relative) if absolute else relative
    target = tmp_path / "copied.json"
    environment = LocalEnvironment(str(workspace))
    try:
        if component == "directory":
            await _copy_artifact_to_host(environment, source, target)
            assert target.read_text() == '{"selected": 42}'
        else:
            with pytest.raises(OSError):
                await _copy_artifact_to_host(environment, source, target)
            assert not target.exists()
        assert (workspace / "result.json").read_text() == '{"selected": 42}'
        assert (workspace / "actual/result.json").read_text() == '{"other": 7}'
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("absolute", [False, True])
async def test_docker_artifact_transfer_resolves_parent_in_execution_environment(tmp_path, absolute):
    (tmp_path / "actual/child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("actual/child", target_is_directory=True)
    (tmp_path / "actual/result.json").write_text('{"selected": 42}')
    (tmp_path / "result.json").write_text('{"other": 7}')

    class ArtifactTransport(LocalDockerTransport):
        async def __call__(self, *argv, **kwargs):
            if argv[0] == "cp":
                shutil.copyfile(argv[2].split(":", 1)[1], argv[3])
                return ProcessResult(0, b"", b"")
            return await super().__call__(*argv, **kwargs)

    transport = ArtifactTransport(tmp_path, pair_reads=False)
    environment = docker_environment(tmp_path, transport)
    relative = "alias/../result.json"
    source = os.path.join(str(tmp_path), relative) if absolute else relative
    target = tmp_path / "copied.json"
    try:
        await _copy_artifact_to_host(environment, source, target)
        assert target.read_text() == '{"selected": 42}'
        assert (tmp_path / "result.json").read_text() == '{"other": 7}'
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("kind", ["local", "worktree"])
@pytest.mark.parametrize("target_kind", ["relative-escape", "absolute-escape", "file-link", "directory-link", "fifo"])
async def test_artifact_transfer_preserves_local_path_and_regular_file_checks(tmp_path, kind, target_kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = (LocalEnvironment if kind == "local" else WorktreeEnvironment)(str(workspace))
    await environment.setup()
    root = Path(environment.workspace)
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir()
    (outside / "result.json").write_text('{"outside": true}')
    if target_kind == "relative-escape":
        source = f"../{outside.name}/result.json"
    elif target_kind == "absolute-escape":
        source = str(outside / "result.json")
    elif target_kind == "file-link":
        (root / "result.json").symlink_to(outside / "result.json")
        source = str(root / "result.json")
    elif target_kind == "directory-link":
        (root / "artifacts").symlink_to(outside, target_is_directory=True)
        source = str(root / "artifacts" / "result.json")
    else:
        os.mkfifo(root / "result.json")
        source = str(root / "result.json")
    target = tmp_path / "copied.json"
    try:
        with pytest.raises((OSError, PermissionError)):
            await _copy_artifact_to_host(environment, source, target)
        assert not target.exists()
    finally:
        for name in ("result.json", "artifacts"):
            if os.path.lexists(root / name):
                (root / name).unlink()
        shutil.rmtree(outside)
        if isinstance(environment, WorktreeEnvironment):
            await environment.get_diff()
        await environment.cleanup()


async def test_artifact_transfer_rejects_replaced_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = LocalEnvironment(str(workspace))
    workspace.rename(tmp_path / "original")
    workspace.mkdir()
    (workspace / "result.json").write_text('{"replacement": true}')
    try:
        with pytest.raises(OSError, match="workspace was replaced"):
            await _copy_artifact_to_host(environment, str(workspace / "result.json"), tmp_path / "copied.json")
        assert not (tmp_path / "copied.json").exists()
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("kind", ["docker", "vm"])
@pytest.mark.parametrize("symlink", [False, True])
async def test_remote_artifact_transfer_uses_existing_transport_and_requires_regular_output(
    tmp_path, monkeypatch, kind, symlink,
):
    environment = object.__new__(DockerEnvironment if kind == "docker" else VMEnvironment)
    environment.workspace = "/workspace"
    target = tmp_path / "copied.json"
    native = tmp_path / "native.json"
    native.write_bytes(b'{"payload":"' + b"x" * (4 * 1024 * 1024) + b'"}\n')

    async def copy(destination):
        if symlink:
            destination.symlink_to(native)
        else:
            shutil.copyfile(native, destination)

    if kind == "docker":
        environment._container_id = "owned-test-container"
        monkeypatch.setattr(environment, "setup", AsyncMock())
        contained = AsyncMock(return_value="/workspace/physical/result.json")
        monkeypatch.setattr(environment, "_contained_file_path", contained)

        async def docker(*args):
            assert args[:3] == ("cp", "--", "owned-test-container:/workspace/physical/result.json")
            await copy(Path(args[3]))
            return ExecResult(0, "", "")

        monkeypatch.setattr(environment, "_docker", docker)
    else:
        async def download(source, destination, *, directory=False):
            assert source == "/workspace/result.json" and not directory
            await copy(destination)

        monkeypatch.setattr(environment, "copy_to_host", download)
    if symlink:
        with pytest.raises(OSError, match="regular file"):
            await _copy_artifact_to_host(environment, "/workspace/result.json", target)
    else:
        await _copy_artifact_to_host(environment, "/workspace/result.json", target)
        assert target.read_bytes() == native.read_bytes()
    if kind == "docker":
        contained.assert_awaited_once_with("/workspace/result.json")
    with pytest.raises(PermissionError, match="escapes workspace"):
        await _copy_artifact_to_host(environment, "/outside/result.json", target)


@pytest.mark.parametrize("kind", ["docker", "vm"])
async def test_missing_remote_artifact_stays_distinguishable_from_transport_failure(
    tmp_path, monkeypatch, kind,
):
    environment = object.__new__(DockerEnvironment if kind == "docker" else VMEnvironment)
    environment.workspace = "/workspace"
    target = tmp_path / "copied.json"

    async def absent(command, timeout=120.0, **kwargs):
        assert command == "test ! -e /workspace/result.json"
        return ExecResult(0, "", "")

    monkeypatch.setattr(environment, "exec_cmd", absent)
    if kind == "docker":
        environment._container_id = "owned-test-container"
        monkeypatch.setattr(environment, "setup", AsyncMock())
        monkeypatch.setattr(environment, "_contained_file_path", AsyncMock(return_value="/workspace/result.json"))

        async def docker(*args):
            return ExecResult(1, "", "No such file")

        monkeypatch.setattr(environment, "_docker", docker)
    else:
        async def download(source, destination, *, directory=False):
            raise VMTransportError("missing", operation_id="download")

        monkeypatch.setattr(environment, "copy_to_host", download)
    with pytest.raises(FileNotFoundError):
        await _copy_artifact_to_host(environment, "/workspace/result.json", target)
    assert not target.exists()

    async def present(command, timeout=120.0, **kwargs):
        return ExecResult(1, "", "")

    monkeypatch.setattr(environment, "exec_cmd", present)
    with pytest.raises((OSError, VMTransportError), match="copy failed|missing"):
        await _copy_artifact_to_host(environment, "/workspace/result.json", target)


@pytest.mark.parametrize("probe", [
    ExecResult(1, "", "Cannot connect to the Docker daemon"),
    ExecResult(0, "", "", timed_out=True),
    ExecResult(0, "", "", stdout_truncated=True),
    ExecResult(0, "", "", stderr_truncated=True),
    ExecResult(0, "", "", stdout_dropped_bytes=1),
    ExecResult(0, "", "", stderr_dropped_bytes=1),
])
async def test_failed_remote_absence_probe_preserves_transfer_error(tmp_path, monkeypatch, probe):
    environment = object.__new__(VMEnvironment)
    environment.workspace = "/workspace"
    monkeypatch.setattr(environment, "exec_cmd", AsyncMock(return_value=probe))
    error = VMTransportError("download failed", operation_id="download")
    monkeypatch.setattr(environment, "copy_to_host", AsyncMock(side_effect=error))

    with pytest.raises(VMTransportError) as caught:
        await _copy_artifact_to_host(environment, "/workspace/result.json", tmp_path / "copied.json")

    assert caught.value is error


async def test_authored_docker_transport_failure_retains_native_artifacts(tmp_path, monkeypatch):
    native = tmp_path / "workspace" / "child"
    native.mkdir(parents=True)
    saved = tmp_path / "saved"
    saved.mkdir()
    (native / "workflow.json").write_text('{"evidence": "retain this workflow"}')
    (native / "result.json").write_text(json.dumps({
        "status": "completed", "output": 42, "tokens": 0, "artifact_files": ["workflow.json"],
    }))
    environment = DockerEnvironment(workspace=str(native.parent))
    environment._container_id = "owned-test-container"
    monkeypatch.setattr(environment, "setup", AsyncMock(return_value=environment._container_id))
    monkeypatch.setattr(environment, "_contained_file_path", AsyncMock(side_effect=lambda path: path))
    monkeypatch.setattr(environment, "read_file", AsyncMock(side_effect=FileNotFoundError))
    commands = []

    async def docker(*args, **kwargs):
        if args[0] == "cp":
            source = args[2].split(":", 1)[1]
            if source.endswith("/workflow.json"):
                return ProcessResult(1, b"", b"Cannot connect to the Docker daemon")
            shutil.copyfile(source, args[3])
            return ProcessResult(0, b"", b"")
        assert args[0] == "exec"
        command = args[-1]
        commands.append(command)
        if command.startswith("test "):
            return ProcessResult(1, b"", b"Cannot connect to the Docker daemon")
        if command.startswith("rm -rf -- "):
            shutil.rmtree(native)
        return ProcessResult(0, b"", b"")

    monkeypatch.setattr(environment, "_docker", docker)
    parent = SimpleNamespace(runner=SimpleNamespace(_run_cancellation_requested=lambda: False))
    tool = AuthoredWorkflowTool(lambda: parent, None, str(saved))
    bridge = SimpleNamespace(tokens=0, poll=AsyncMock(), aclose=AsyncMock())
    child = _Child("child", environment, str(native), None, bridge=bridge)
    tool._children[child.id] = child

    await tool._execute(child, "fixture-runner", b"{}")

    assert child.status == "failed"
    assert isinstance(child.cleanup_error, OSError)
    assert "child result persistence failed" in child.result["reason"]
    with pytest.raises(RuntimeError, match="Docker source workspace copy failed"):
        await tool.aclose()
    assert (native / "workflow.json").is_file()
    assert not child.artifacts_removed
    assert all(not command.startswith("rm -rf -- ") for command in commands)


async def test_artifact_copy_failure_closes_source_descriptor(tmp_path, monkeypatch):
    (tmp_path / "result.json").write_text('{"ready": true}')
    environment = LocalEnvironment(str(tmp_path))
    descriptor = None

    def fail_copy(source, target):
        nonlocal descriptor
        descriptor = source.fileno()
        raise OSError("destination write failed")

    monkeypatch.setattr(snapshot_module.shutil, "copyfileobj", fail_copy)
    try:
        with pytest.raises(OSError, match="destination write failed"):
            await _copy_artifact_to_host(environment, "result.json", tmp_path / "copied.json")
        assert descriptor is not None
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        await environment.cleanup()


async def test_artifact_transfer_retains_bridge_environment_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifacts = tmp_path / "bridge-artifacts"
    artifacts.mkdir()
    (tmp_path / "result.json").write_text('{"ready": true}')
    environment = LocalEnvironment(str(tmp_path))
    bridge = AuthoredWorkflowBridge(
        environment, str(artifacts),
        SessionEnvironmentPool(str(tmp_path), isolation="shared", base_environment=environment),
        {}, ToolRuntime(environment, None, None), allow_unisolated_shell=True,
    )
    proxy = BridgeEnvironment(BridgeClient(artifacts, bridge.options()), {
        "handle": 0, "workspace": str(tmp_path), "process_isolated": False,
    })
    copying = asyncio.create_task(_copy_artifact_to_host(proxy, "result.json", tmp_path / "copied.json"))

    async def poll_until_copied():
        while not copying.done():
            await bridge.poll()
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(poll_until_copied(), 5)
        await copying
        assert (tmp_path / "copied.json").read_text() == (tmp_path / "result.json").read_text()
    finally:
        copying.cancel()
        await asyncio.gather(copying, return_exceptions=True)
        await bridge.aclose()
        await environment.cleanup()
