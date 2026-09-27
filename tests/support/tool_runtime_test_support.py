"""Shared fakes for the tool contract tests.

``test_tool_runtime_contract.py``, ``test_file_tool_contract.py`` and
``test_search_tool_contract.py`` were split out of a single oversized module;
these are the module-level doubles they build their runtimes from.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")

    async def read_file(self, path: str) -> str:
        raise AssertionError("read_file was not expected")

    async def write_file(self, path: str, content: str) -> None:
        raise AssertionError("write_file was not expected")


class FalseyFakeEnv(FakeEnv):
    def __bool__(self) -> bool:
        return False

    async def read_file(self, path: str) -> str:
        return "contents\n"


class SpySafetyPolicy:
    def __init__(self):
        self.cmd_calls = []
        self.path_calls = []

    def check_path(self, target_path: str) -> str:
        self.path_calls.append(target_path)
        return target_path

    def check_cmd(self, cmd: str) -> None:
        pass

    def is_risky(self, cmd: str) -> bool:
        return False

    async def check_cmd_interactive(self, cmd: str, confirm_fn=None) -> None:
        self.cmd_calls.append((cmd, confirm_fn))


class FakeRemoteEnv:
    """Environment that does I/O somewhere other than the host filesystem."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content


class SpyLock:
    instances: list["SpyLock"] = []

    def __init__(self, *args, **kwargs):
        SpyLock.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
