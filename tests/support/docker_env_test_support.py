"""Docker command doubles shared by environment tests."""

from collections.abc import Callable

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_process import ProcessResult

CONTAINER_ID = "a" * 64


def _result(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    *,
    stdout_dropped: int = 0,
    stderr_dropped: int = 0,
) -> ProcessResult:
    return ProcessResult(
        returncode,
        stdout,
        stderr,
        stdout_dropped_bytes=stdout_dropped,
        stderr_dropped_bytes=stderr_dropped,
    )


class FakeDocker:
    def __init__(self, handler: Callable | None = None) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    async def __call__(self, command, **kwargs):
        command = tuple(command)
        self.calls.append((command, kwargs))
        if self.handler is None:
            return _result()
        value = self.handler(command, kwargs)
        if isinstance(value, BaseException):
            raise value
        return value


def _patch(monkeypatch, fake: FakeDocker) -> None:
    monkeypatch.setattr(docker_module, "run_process", fake)
