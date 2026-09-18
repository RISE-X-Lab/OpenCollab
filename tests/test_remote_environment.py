from __future__ import annotations

import asyncio

import httpx
import pytest

from opencollab.adapters.execution.remote import RemoteEnvironment


async def test_remote_environment_preserves_execution_and_file_contract() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/v1/environments":
            return httpx.Response(
                200,
                json={"id": "env-1", "workspace": "/workspace", "generation": 3},
            )
        if path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "ignored", "state": "running"})
        if path.startswith("/v1/executions/"):
            return httpx.Response(
                200,
                json={
                    "state": "completed",
                    "result": {
                        "returncode": 4,
                        "stdout": "out\n",
                        "stderr": "err\n",
                        "stdout_truncated": True,
                        "stderr_truncated": False,
                        "stdout_dropped_bytes": 7,
                        "stderr_dropped_bytes": 0,
                    },
                },
            )
        if path.endswith("/files/read"):
            return httpx.Response(200, json={"content": "hello"})
        return httpx.Response(200, json={})

    environment = RemoteEnvironment(
        "http://execution.example:8080",
        image="python:3.11-slim",
        transport=httpx.MockTransport(respond),
    )
    assert await environment.setup() == "/workspace"
    result = await environment.exec_cmd("printf out; printf err >&2; exit 4")
    assert result.returncode == 4
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.stdout_truncated is True
    assert result.stdout_dropped_bytes == 7
    assert await environment.read_file("README.md") == "hello"
    await environment.write_file("result.txt", "done")
    await environment.cleanup()

    guarded = [request for request in calls if "/environments/env-1/" in request.url.path]
    assert guarded
    assert all(request.headers["X-Sandbox-Generation"] == "3" for request in guarded)
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/v1/environments/env-1"


async def test_cancel_during_submission_waits_for_remote_quiescence() -> None:
    submitted = asyncio.Event()
    cancelled: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/executions"):
            submitted.set()
            await asyncio.sleep(30)
        if request.method == "DELETE" and "/executions/" in request.url.path:
            cancelled.append(request.url.path)
            return httpx.Response(200, json={"state": "cancelled", "quiesced": True})
        return httpx.Response(200, json={})

    environment = RemoteEnvironment(
        "http://execution.example:8080",
        image="python:3.11-slim",
        transport=httpx.MockTransport(respond),
    )
    environment._id = "env-1"
    task = asyncio.create_task(environment.exec_cmd("sleep 30"))
    await submitted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(cancelled) == 1
    assert "/v1/executions/exec_" in cancelled[0]


async def test_missing_cancel_handle_does_not_claim_remote_quiescence() -> None:
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            404,
            json={"detail": {"code": "execution_not_found", "message": "not registered"}},
        )

    environment = RemoteEnvironment(
        "http://execution.example:8080",
        image="python:3.11-slim",
        transport=httpx.MockTransport(respond),
    )
    environment._id = "env-1"

    with pytest.raises(RuntimeError, match="could not confirm remote quiescence"):
        await environment._confirm_cancelled("exec-late", attempts=2)
    assert attempts == 2


async def test_torn_down_error_revokes_client_before_another_submission() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            409,
            json={"detail": {"code": "sandbox_torn_down", "message": "sandbox stopped"}},
        )

    environment = RemoteEnvironment(
        "http://execution.example:8080",
        image="python:3.11-slim",
        transport=httpx.MockTransport(respond),
    )
    environment._id = "env-1"
    with pytest.raises(RuntimeError, match="sandbox stopped"):
        await environment.exec_cmd("first")
    with pytest.raises(RuntimeError, match="revoked"):
        await environment.exec_cmd("second")
    assert calls == 1
