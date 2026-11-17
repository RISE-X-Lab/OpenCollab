from __future__ import annotations

import asyncio

import pytest

from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.tools.base import host_write_locks
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from tests.runtime.test_vm_environment import _env


@pytest.mark.parametrize("cancel", [False, True])
async def test_vm_grouped_mutation_uses_one_native_workspace_lock(tmp_path, monkeypatch, cancel):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/value.py").write_text("VALUE = 1\n")
    native = LocalEnvironment(str(tmp_path))
    environment = _env()
    environment.workspace = str(tmp_path)
    environment._identity = {"workspace": str(tmp_path)}
    lock = asyncio.Lock()
    owners = set()
    acquisitions = []

    async def rpc(payload):
        operation = payload["operation_id"]
        if payload["method"] == "cancel":
            if operation in owners:
                owners.remove(operation)
                lock.release()
            return {"status": "cancelled"}
        assert payload["method"] == "start"
        action = payload["request"]["action"]
        arguments = payload["request"]["args"]
        if action == "lock":
            await lock.acquire()
            owners.add(operation)
            acquisitions.append(operation)
            return {"status": "running", "lock_acquired": True}
        assert lock.locked()
        if action == "read":
            value = await native.read_file(arguments["path"])
        else:
            assert action == "write"
            value = await native.write_file_with_change(arguments["path"], arguments["content"])
        return {"status": "completed", "value": value}

    monkeypatch.setattr(environment, "_rpc", rpc)
    entered, release = asyncio.Event(), asyncio.Event()

    async def publish():
        async with host_write_locks((
            str(tmp_path / ".candidate-adoption"), str(tmp_path / "nested/value.py"),
            str(tmp_path / "unrelated.py"),
        ), environment):
            await environment.write_file("nested/value.py", "VALUE = 42\n")
            entered.set()
            await release.wait()

    publication = asyncio.create_task(publish())
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(acquisitions) == 1
        writer = asyncio.create_task(FileWriteTool().execute_with_runtime({
            "path": "nested/value.py", "mode": "create", "content": "VALUE = 100\n",
        }, ToolRuntime(environment, None, None)))
        await asyncio.sleep(0.05)
        assert not writer.done()
        if cancel:
            publication.cancel()
            with pytest.raises(asyncio.CancelledError):
                await publication
        else:
            release.set()
            await publication
        assert "Created/wrote" in await asyncio.wait_for(writer, 2)
        assert (tmp_path / "nested/value.py").read_text() == "VALUE = 100\n"
        assert len(acquisitions) == 2
        assert not owners
        assert not environment.pending_operations
        assert not environment.revoked
    finally:
        release.set()
        await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)
        await environment.cleanup()
        await native.cleanup()
