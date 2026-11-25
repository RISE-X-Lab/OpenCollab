from __future__ import annotations

from dataclasses import asdict

from opencollab.adapters import _vm_remote
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment, VMEnvironment
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.adapters.tools.fs import GrepTool
from opencollab.adapters.vm_config import VMConnection
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.authored_workflow_bridge import AuthoredWorkflowBridge, BridgeEnvironment
from opencollab.bootstrap.runtime_context import build_workspace_safety_policy
from tests.support.docker_native_edits_support import RemoteDockerTransport


async def test_vm_search_path_is_resolved_in_the_guest(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "nested").mkdir(parents=True)
    (workspace / "module.py").write_text("search marker\n")
    guest = LocalEnvironment(str(workspace))
    vm = VMEnvironment(
        VMConnection("guest", "worker"), str(tmp_path / "guest"), state_directory=str(tmp_path / "state"),
    )
    info = workspace.stat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    vm._identity = identity
    monkeypatch.setattr(_vm_remote, "_identity", lambda path: identity)
    calls = []

    async def rpc(payload):
        request = payload["request"]
        action, args = request["action"], request["args"]
        calls.append(action)
        if action == "resolve_search_path":
            path = args["path"].replace(vm.workspace, str(workspace), 1)
            value = await _vm_remote._perform({
                **request, "workspace": str(workspace), "args": {"path": path},
            }, tmp_path)
            value = value.replace(str(workspace), vm.workspace, 1)
        else:
            assert action == "exec"
            command = args["cmd"].replace(vm.workspace, str(workspace), 1)
            value = {**asdict(await guest.exec_cmd(command)), "stdout_bytes": None}
        return {"status": "completed", "value": value}

    monkeypatch.setattr(vm, "_rpc", rpc)
    runtime = ToolRuntime(vm, build_workspace_safety_policy(vm), None)
    try:
        result = await GrepTool().execute_with_runtime(
            {"path": "nested/../module.py", "pattern": "search marker"}, runtime,
        )
        assert result == "1:search marker"
        assert calls[:2] == ["resolve_search_path", "exec"]
    finally:
        await guest.cleanup()
        await vm.cleanup()


async def test_authored_bridge_preserves_remote_search_resolution(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "module.py").write_text("search marker\n")
    transport = RemoteDockerTransport(tmp_path)
    environment = DockerEnvironment(
        workspace=transport.remote, exec_workdir=transport.remote, container_id="b" * 64,
    )
    transport.attach(environment)
    pool = SessionEnvironmentPool(environment.workspace, isolation="shared", base_environment=environment)
    bridge = AuthoredWorkflowBridge(
        environment, environment.workspace + "/artifacts", pool, {}, ToolRuntime(environment, None, None),
        allow_unisolated_shell=True,
    )

    class BridgeConnection:
        async def call(self, action, arguments, handle, *, edit=None):
            return await bridge._dispatch("local-search", {
                "action": action, "arguments": arguments, "environment": handle, "edit": edit,
            })

    client = BridgeConnection()
    handle = await client.call("acquire", {"strategy": "shared", "label": "child"}, 0)
    proxy = BridgeEnvironment(client, handle)
    try:
        runtime = ToolRuntime(proxy, build_workspace_safety_policy(proxy), None)
        result = await GrepTool().execute_with_runtime(
            {"path": "nested/..", "pattern": "search marker"}, runtime,
        )
        assert "module.py:1:search marker" in result
    finally:
        await proxy.cleanup()
        await bridge.aclose()
        await environment.cleanup()
