"""Cleanup failure must survive the HTTP boundary and remain retryable."""
from types import SimpleNamespace

import httpx

from opencollab.adapters.execution import service as server


async def test_destroy_failure_returns_error_and_preserves_retry(monkeypatch):
    class Backend:
        calls = 0

        async def destroy(self, sandbox):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("injected cgroup kill failure")

    backend = Backend()
    env = SimpleNamespace(revoke=lambda: None)
    record = server._EnvRecord(SimpleNamespace(env=env))
    monkeypatch.setattr(server, "BACKEND", backend)
    monkeypatch.setattr(server, "_ENVS", {"test-env": record})
    monkeypatch.setattr(server, "_EXECS", {})
    monkeypatch.setattr(server, "_CHECKPOINTS", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                base_url="http://test") as client:
        first = await client.delete("/v1/environments/test-env")
        assert first.status_code == 503
        assert first.json()["detail"]["code"] == "cleanup_failed"
        assert server._ENVS["test-env"] is record
        assert not record.alive
        retry = await client.delete("/v1/environments/test-env")
        assert retry.status_code == 200
        assert retry.json()["state"] == "destroyed"
        assert "test-env" not in server._ENVS
        assert backend.calls == 2


async def test_reaped_torn_down_environment_keeps_stable_409(monkeypatch):
    class Backend:
        async def destroy(self, sandbox):
            pass

    env = SimpleNamespace(revoke=lambda: None)
    record = server._EnvRecord(SimpleNamespace(env=env))
    record.alive = False
    monkeypatch.setattr(server, "BACKEND", Backend())
    monkeypatch.setattr(server, "_ENVS", {"dead-env": record})
    monkeypatch.setattr(server, "_EXECS", {})
    monkeypatch.setattr(server, "_CHECKPOINTS", {})
    monkeypatch.setattr(server, "_TORN_DOWN", {})
    await server._reclaim_env("dead-env", record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                base_url="http://test") as client:
        response = await client.post("/v1/environments/dead-env/executions",
                                     json={"command": "true"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sandbox_torn_down"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                base_url="http://test") as client:
        deleted = await client.delete("/v1/environments/dead-env")
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "destroyed"
    assert "dead-env" not in server._TORN_DOWN
