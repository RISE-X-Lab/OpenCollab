"""Caller-provided execution IDs preserve environment and command identity."""

import asyncio
import os
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.external_server,
    pytest.mark.skipif(
        os.environ.get("OPENCOLLAB_TEST_EXECUTION_SERVER") != "1",
        reason="set OPENCOLLAB_TEST_EXECUTION_SERVER=1 with a server on port 8080",
    ),
]


@pytest.fixture
async def environments():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080/v1", trust_env=False, timeout=120) as client:
        ids = []
        try:
            for _ in range(2):
                response = await client.post(
                    "/environments", json={"image": os.environ.get("EXECSERVER_TEST_IMAGE", "python:3.11-slim")}
                )
                response.raise_for_status()
                ids.append(response.json()["id"])
            yield client, ids
        finally:
            for env_id in ids:
                response = await client.delete(f"/environments/{env_id}")
                assert response.status_code == 200


@pytest.mark.parametrize("conflict", ["environment", "command", "timeout"])
async def test_execution_id_rejects_conflicting_reuse(environments, conflict):
    client, (owner, other) = environments
    body = {"execution_id": "exec_" + uuid.uuid4().hex, "command": "printf original", "timeout": 30}
    response = await client.post(f"/environments/{owner}/executions", json=body)
    response.raise_for_status()
    completed = await client.get(f"/executions/{body['execution_id']}")
    assert completed.json()["result"]["stdout"] == "original"
    retry = dict(body)
    target = owner
    if conflict == "environment":
        target = other
    elif conflict == "command":
        retry["command"] = "printf changed"
    else:
        retry["timeout"] = 31
    rejected = await client.post(f"/environments/{target}/executions", json=retry)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "execution_id_conflict"
    unchanged = await client.get(f"/executions/{body['execution_id']}")
    assert unchanged.json()["result"]["stdout"] == "original"


async def test_exact_execution_id_retries_run_once(environments):
    client, (owner, _) = environments
    body = {
        "execution_id": "exec_" + uuid.uuid4().hex,
        "command": "printf x >> /workspace/count; sleep 1",
        "timeout": 30,
    }
    responses = await asyncio.gather(*[
        client.post(f"/environments/{owner}/executions", json=body) for _ in range(8)
    ])
    assert all(r.status_code == 200 for r in responses)
    assert {r.json()["execution_id"] for r in responses} == {body["execution_id"]}
    completed = await client.get(f"/executions/{body['execution_id']}")
    assert completed.json()["result"]["returncode"] == 0
    retry = await client.post(f"/environments/{owner}/executions", json=body)
    assert retry.status_code == 200
    result = await client.post(f"/environments/{owner}/files/read", json={"path": "/workspace/count"})
    assert result.json()["content"] == "x"
