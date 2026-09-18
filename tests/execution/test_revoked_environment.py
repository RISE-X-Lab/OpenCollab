"""Revocation during an operation must fence later HTTP admissions."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from opencollab.adapters.execution import service as server


@pytest.fixture
async def revoked_rig(monkeypatch):
    class Env:
        revoked = False
        workspace = '/workspace'

        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def exec_cmd(self, command, timeout):
            self.calls.append(command)
            self.entered.set()
            await self.release.wait()
            self.revoked = True
            raise RuntimeError('injected cleanup failure')

        async def read_file(self, path):
            self.calls.append(path)
            self.revoked = True
            raise RuntimeError('injected read cleanup failure')

        def revoke(self):
            self.revoked = True

    env = Env()
    backend = SimpleNamespace(alive=lambda sb: not sb.env.revoked)
    record = server._EnvRecord(SimpleNamespace(env=env))
    monkeypatch.setattr(server, 'BACKEND', backend)
    monkeypatch.setattr(server, '_ENVS', {'e': record})
    monkeypatch.setattr(server, '_EXECS', {})
    monkeypatch.setattr(server, '_CHECKPOINTS', {})
    monkeypatch.setattr(server, '_TORN_DOWN', {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        yield client, env, record


async def test_revocation_fences_queued_and_new_commands(revoked_rig):
    client, env, record = revoked_rig
    first = await client.post('/v1/environments/e/executions', json={'command': 'timeout'})
    await env.entered.wait()
    queued = await client.post('/v1/environments/e/executions', json={'command': 'must-not-run'})
    env.release.set()
    for response in (first, queued):
        result = await client.get('/v1/executions/' + response.json()['execution_id'])
        assert result.status_code == 409, result.text
        assert result.json()['detail']['code'] == 'sandbox_torn_down'
    assert not record.alive
    assert env.calls == ['timeout']
    for path, body in [
        ('executions', {'command': 'later'}),
        ('files/read', {'path': 'later'}),
        ('checkpoints', {}),
        ('heartbeat', {}),
    ]:
        rejected = await client.post('/v1/environments/e/' + path, json=body)
        assert rejected.status_code == 409
    assert env.calls == ['timeout']


async def test_file_operation_revocation_updates_state(revoked_rig):
    client, env, record = revoked_rig
    response = await client.post('/v1/environments/e/files/read', json={'path': 'trigger'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'sandbox_torn_down'
    assert not record.alive
    response = await client.post('/v1/environments/e/executions', json={'command': 'later'})
    assert response.status_code == 409
    assert env.calls == ['trigger']


async def test_live_environment_preserves_original_error(revoked_rig):
    client, env, record = revoked_rig

    async def missing(path):
        raise FileNotFoundError(path)

    env.read_file = missing
    response = await client.post('/v1/environments/e/files/read', json={'path': 'missing'})
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'not_found'
    assert record.alive
