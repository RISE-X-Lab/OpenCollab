"""Deterministic HTTP lifecycle races, using events rather than timing sleeps."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from opencollab.adapters.execution import service as server


@pytest.fixture
async def rig(monkeypatch):
    class Env:
        workspace = "/workspace"

        def __init__(self):
            self.calls = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.block_write = False

        async def write_file(self, path, content):
            self.entered.set()
            if self.block_write:
                await self.release.wait()
            self.calls.append((path, content))

        async def exec_cmd(self, command, timeout):
            self.calls.append(command)
            return {"returncode": 0}

        def revoke(self):
            pass

    old, new = SimpleNamespace(env=Env()), SimpleNamespace(env=Env())

    class Backend:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.destroyed = []
            self.fail = False

        async def materialize(self, ref):
            self.entered.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("injected materialize failure")
            return new

        async def destroy(self, sb):
            self.destroyed.append(sb)

        async def discard(self, ref):
            pass

    backend = Backend()
    class ObservedLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.queued = asyncio.Event()

        async def acquire(self):
            if self.locked():
                self.queued.set()
            return await super().acquire()

    rec = server._EnvRecord(old)
    rec.snapshot_lock = ObservedLock()
    monkeypatch.setattr(server, "BACKEND", backend)
    monkeypatch.setattr(server, "_ENVS", {"e": rec})
    monkeypatch.setattr(server, "_EXECS", {})
    monkeypatch.setattr(server, "_CHECKPOINTS", {
        "c": server._CheckpointRecord("c", "e", "/workspace", [], None),
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app,
                                raise_app_exceptions=False), base_url="http://test") as client:
        yield client, rec, backend, old, new


RESTORE = "/v1/environments/e/checkpoints/c/restore"
WRITE = "/v1/environments/e/files/write"
EXEC = "/v1/environments/e/executions"


@pytest.mark.parametrize("path,body", [
    (WRITE, {"path": "x", "content": "late"}),
    (EXEC, {"command": "late"}),
    (RESTORE, {}),
])
async def test_restore_rejects_new_admissions(rig, path, body):
    client, rec, backend, old, new = rig
    task = asyncio.create_task(client.post(RESTORE))
    await asyncio.wait_for(backend.entered.wait(), 2)
    try:
        response = await asyncio.wait_for(
            client.post(path, json=body, headers={"X-Sandbox-Generation": "0"}), 2)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "sandbox_restoring"
        assert old.env.calls == new.env.calls == []
    finally:
        backend.release.set()
        await asyncio.wait_for(task, 2)
    assert rec.generation == 1


async def test_restore_waits_for_inflight_file_operation(rig):
    client, rec, backend, old, new = rig
    old.env.block_write = True
    write = asyncio.create_task(client.post(WRITE, json={"path": "x", "content": "before"}))
    await asyncio.wait_for(old.env.entered.wait(), 2)
    restore = asyncio.create_task(client.post(RESTORE))
    await asyncio.wait_for(rec.snapshot_lock.queued.wait(), 2)
    try:
        assert not backend.entered.is_set()
    finally:
        old.env.release.set()
        backend.release.set()
        await asyncio.wait_for(asyncio.gather(write, restore), 2)
    assert old.env.calls == [("x", "before")]
    assert new.env.calls == []
    assert rec.generation == 1


async def test_stale_restore_header_cannot_restore_twice(rig):
    client, rec, backend, old, new = rig
    backend.release.set()
    assert (await client.post(RESTORE, headers={"X-Sandbox-Generation": "0"})).status_code == 200
    stale = await client.post(RESTORE, headers={"X-Sandbox-Generation": "0"})
    assert stale.status_code == 409
    assert rec.generation == 1
    current = await client.post(WRITE, json={"path": "x", "content": "current"},
                                headers={"X-Sandbox-Generation": "1"})
    assert current.status_code == 200
    assert new.env.calls == [("x", "current")]


async def test_materialize_failure_does_not_publish_generation(rig):
    client, rec, backend, old, new = rig
    backend.fail = True
    backend.release.set()
    assert (await client.post(RESTORE)).status_code == 500
    assert rec.sb is old and rec.generation == 0
    response = await client.post(WRITE, json={"path": "x", "content": "retry"})
    assert response.status_code == 200


async def test_queued_execution_generation_error_is_409(rig):
    client, rec, backend, old, new = rig
    await rec.snapshot_lock.acquire()
    try:
        submitted = await client.post(EXEC, json={"command": "must-not-run"})
        execution_id = submitted.json()["execution_id"]
        rec.generation += 1
    finally:
        rec.snapshot_lock.release()
    result = await client.get(f"/v1/executions/{execution_id}")
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "stale_generation"
    assert old.env.calls == new.env.calls == []


async def test_queued_file_rechecks_generation(rig):
    client, rec, backend, old, new = rig
    await rec.snapshot_lock.acquire()
    task = asyncio.create_task(client.post(WRITE, json={"path": "x", "content": "stale"}))
    try:
        await asyncio.wait_for(rec.snapshot_lock.queued.wait(), 2)
        rec.sb = new
        rec.generation += 1
    finally:
        rec.snapshot_lock.release()
    response = await asyncio.wait_for(task, 2)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_generation"
    assert old.env.calls == new.env.calls == []


async def test_restore_keeps_admission_closed_until_old_sandbox_destroyed(rig):
    client, rec, backend, old, new = rig
    entered, release = asyncio.Event(), asyncio.Event()

    async def destroy(sb):
        assert sb is old
        entered.set()
        await release.wait()

    backend.destroy = destroy
    backend.release.set()
    task = asyncio.create_task(client.post(RESTORE))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert rec.sb is old and rec.generation == 0
        assert (await client.post(EXEC, json={"command": "late"})).status_code == 409
        assert (await client.delete("/v1/environments/e")).status_code == 409
    finally:
        release.set()
        result = await asyncio.wait_for(task, 2)
    assert result.status_code == 200
    assert rec.sb is new and rec.generation == 1


async def test_failed_old_sandbox_cleanup_never_publishes_replacement(rig):
    client, rec, backend, old, new = rig

    async def destroy(sb):
        if sb is old:
            raise RuntimeError("injected old sandbox cleanup failure")
        backend.destroyed.append(sb)

    backend.destroy = destroy
    backend.release.set()
    assert (await client.post(RESTORE)).status_code == 500
    assert rec.sb is old and rec.generation == 0 and not rec.alive
    assert backend.destroyed == [new]
    assert (await client.post(EXEC, json={"command": "unsafe"})).status_code == 409

async def test_cancelled_restore_request_does_not_cancel_transition(rig):
    client, rec, backend, old, new = rig
    task = asyncio.create_task(server.restore("e", "c", None))
    await asyncio.wait_for(backend.entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert rec.restoring and rec.sb is old
    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert not rec.restoring
    assert rec.sb is new and rec.generation == 1
    assert backend.destroyed == [old]


async def test_repeated_cancel_during_cleanup_still_finishes_transition(rig):
    client, rec, backend, old, new = rig
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()

    async def destroy(sb):
        cleanup_entered.set()
        await cleanup_release.wait()
        backend.destroyed.append(sb)

    backend.destroy = destroy
    backend.release.set()
    task = asyncio.create_task(server.restore("e", "c", None))
    await asyncio.wait_for(cleanup_entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert backend.destroyed == [old]
    assert rec.sb is new and rec.generation == 1 and not rec.restoring


async def test_failed_replacement_cleanup_is_retained_for_delete_retry(rig):
    client, rec, backend, old, new = rig
    failures = {id(old): 1, id(new): 1}

    async def destroy(sb):
        remaining = failures[id(sb)]
        if remaining:
            failures[id(sb)] -= 1
            raise RuntimeError(f"injected cleanup failure for {id(sb)}")
        backend.destroyed.append(sb)

    backend.destroy = destroy
    backend.release.set()
    response = await client.post(RESTORE)
    assert response.status_code == 500
    assert rec.sb is old and not rec.alive
    assert rec.pending_cleanup == [new]

    retry = await client.delete("/v1/environments/e")
    assert retry.status_code == 200
    assert backend.destroyed == [old, new]
    assert "e" not in server._ENVS

@pytest.mark.parametrize('hard_cancel', [False, True])
async def test_restore_cancels_active_execution_then_materializes(rig, hard_cancel):
    client, rec, backend, old, new = rig
    exec_entered, exec_quiet = asyncio.Event(), asyncio.Event()

    async def running(command, timeout):
        exec_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            if hard_cancel:
                old.env.revoked = True
            exec_quiet.set()

    old.env.exec_cmd = running
    started = await client.post(EXEC, json={"command": "long"})
    execution_id = started.json()["execution_id"]
    await asyncio.wait_for(exec_entered.wait(), 2)
    backend.release.set()
    response = await client.post(RESTORE)
    assert response.status_code == 200
    assert exec_quiet.is_set()
    assert rec.alive
    admitted = await client.post(EXEC, json={'command': 'after-restore'})
    assert admitted.status_code == 200, admitted.text
    completed = await client.get('/v1/executions/' + admitted.json()['execution_id'])
    assert completed.status_code == 200, completed.text
    assert new.env.calls == ['after-restore']
    assert execution_id not in server._EXECS
    assert rec.sb is new and rec.generation == 1

async def test_execution_cleanup_failure_makes_restore_fail_closed(rig):
    client, rec, backend, old, new = rig
    exec_entered = asyncio.Event()

    async def cleanup_fails(command, timeout):
        exec_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("cannot prove execution quiesced") from exc

    old.env.exec_cmd = cleanup_fails
    await client.post(EXEC, json={"command": "long"})
    await asyncio.wait_for(exec_entered.wait(), 2)
    response = await client.post(RESTORE)
    assert response.status_code == 500
    assert not rec.alive and rec.sb is old and rec.generation == 0
    assert not backend.entered.is_set()
    assert (await client.post(EXEC, json={"command": "unsafe"})).status_code == 409
