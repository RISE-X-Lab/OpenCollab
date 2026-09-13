"""Slow daemon disposal must fit the finite control budget."""

import asyncio

import pytest
from test_docker_env import CONTAINER_ID, FakeDocker, _patch, _result

from opencollab.adapters.env import DockerEnvironment


def test_disposal_after_twenty_seconds_does_not_lose_container_ownership(monkeypatch):
    def respond(command, kwargs):
        if command[1] == "run":
            return _result(stdout=f"{CONTAINER_ID}\n".encode())
        assert command[1:] == ("rm", "-f", "--", CONTAINER_ID)
        # Simulate a daemon whose filesystem cleanup takes twenty seconds.
        if kwargs["timeout"] <= 20:
            raise asyncio.TimeoutError("daemon is still disposing filesystem resources")
        assert kwargs["timeout"] == 60
        return _result()

    fake = FakeDocker(respond)
    _patch(monkeypatch, fake)

    async def scenario():
        env = DockerEnvironment(image="fixture:latest")
        assert await env.setup() == CONTAINER_ID
        await env.cleanup()
        await env.cleanup()
        assert len([args for args, _ in fake.calls if args[1] == "rm"]) == 1

    asyncio.run(scenario())


def test_explicit_docker_command_timeout_remains_bounded(monkeypatch):
    def respond(command, kwargs):
        assert command == ("docker", "info")
        assert kwargs["timeout"] == 0.25
        raise asyncio.TimeoutError("daemon unavailable")

    _patch(monkeypatch, FakeDocker(respond))
    env = DockerEnvironment(image="fixture:latest")
    with pytest.raises(asyncio.TimeoutError, match="daemon unavailable"):
        asyncio.run(env._docker("info", timeout=0.25))
