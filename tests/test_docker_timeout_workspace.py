"""A timeout must preserve workspace ownership until explicit cleanup."""

import asyncio

import pytest
from test_docker_env import CONTAINER_ID, FakeDocker, _patch, _result

from opencollab.adapters._env_process import ProcessCleanupError
from opencollab.adapters.env import DockerEnvironment


async def test_failed_group_cancel_stops_but_retains_workspace(monkeypatch):
    def respond(command, _kwargs):
        if command[1] == "run":
            assert "--rm" not in command
            return _result(stdout=CONTAINER_ID.encode())
        if command[1] == "exec":
            if "opencollab-cancel" in command:
                return _result(returncode=124)
            return asyncio.TimeoutError()
        if command[1] in {"stop", "rm"}:
            return _result()
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    with pytest.raises(ProcessCleanupError, match="did not quiesce"):
        await env.exec_cmd("sleep 60", timeout=0.01)
    assert env.revoked
    assert env._container_id == CONTAINER_ID
    assert [call[0][1] for call in fake.calls] == ["run", "exec", "exec", "stop"]
    with pytest.raises(RuntimeError, match="aborted"):
        await env.exec_cmd("git diff")
    await env.cleanup()
    assert fake.calls[-1][0] == ("docker", "rm", "-f", "--", CONTAINER_ID)


async def test_abort_retains_container_and_backing_until_cleanup(monkeypatch):
    class Backing:
        cleaned = False

        async def cleanup(self):
            self.cleaned = True

    backing = Backing()
    fake = FakeDocker(lambda command, _kwargs: _result(stdout=CONTAINER_ID.encode()))
    _patch(monkeypatch, fake)
    env = DockerEnvironment(backing_environment=backing)
    await env.setup()
    await env.abort()
    assert env.revoked
    assert env._container_id == CONTAINER_ID
    assert fake.calls[-1][0] == ("docker", "stop", "--time", "1", "--", CONTAINER_ID)
    assert not backing.cleaned
    assert not any(call[0][1] == "rm" for call in fake.calls)
    await env.cleanup()
    assert backing.cleaned
    assert env._container_id is None


async def test_failed_container_stop_never_claims_recovered_timeout(monkeypatch):
    def respond(command, _kwargs):
        if command[1] == "run":
            return _result(stdout=CONTAINER_ID.encode())
        if command[1] == "exec" and "opencollab-exec" in command:
            return asyncio.TimeoutError()
        return _result(returncode=1)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    with pytest.raises(ProcessCleanupError, match="workspace retained"):
        await env.exec_cmd("sleep 60", timeout=0.01)
    assert env.revoked and env._container_id == CONTAINER_ID
    assert not any(call[0][1] == "rm" for call in fake.calls)


async def test_quiesced_command_timeout_keeps_same_container_usable(monkeypatch):
    timed_out = False

    def respond(command, _kwargs):
        nonlocal timed_out
        if command[1] == "run":
            return _result(stdout=CONTAINER_ID.encode())
        if command[1] == "exec":
            if not timed_out:
                timed_out = True
                return asyncio.TimeoutError()
            return _result(stdout=b"preserved patch")
        raise AssertionError(command)

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)
    env = DockerEnvironment()
    await env.setup()
    result = await env.exec_cmd("sleep 60", timeout=0.01)
    assert result.returncode == -1
    assert not env.revoked
    assert (await env.exec_cmd("git diff")).stdout == "preserved patch"
    assert env._container_id == CONTAINER_ID
    assert not any(call[0][1] in {"stop", "rm"} for call in fake.calls)


async def test_caller_cancellation_retains_owned_container(monkeypatch):
    started = asyncio.Event()
    env = DockerEnvironment()

    async def docker(*args, **_kwargs):
        if args[0] == "run":
            return _result(stdout=CONTAINER_ID.encode())
        if args[0] == "exec":
            if "opencollab-cancel" in args:
                return _result(returncode=125)
            started.set()
            await asyncio.Event().wait()
        if args[0] == "stop":
            return _result()
        raise AssertionError(args)

    monkeypatch.setattr(env, "_docker", docker)
    await env.setup()
    task = asyncio.create_task(env.exec_cmd("sleep 60"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.revoked
    assert env._container_id == CONTAINER_ID
    assert not env._active_execs
