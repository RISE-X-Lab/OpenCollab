"""Stable structural environment contract and Docker attachment factory."""

from __future__ import annotations

import posixpath
from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from opencollab.adapters.env import ExecResult

from .errors import InvalidSDKRequestError

CommandResult = ExecResult


@runtime_checkable
class ExecutionEnvironment(Protocol):
    """Environment shape accepted by SDK runtimes and external adapters."""

    workspace: str
    host_workspace: str | None
    source_workspace: str | None
    local_filesystem: bool
    process_isolated: bool

    @property
    @abstractmethod
    def revoked(self) -> bool: ...

    @abstractmethod
    def revoke(self) -> None: ...

    @abstractmethod
    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult: ...

    @abstractmethod
    async def read_file(self, path: str) -> str: ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    async def write_temp_file(self, content: str, *, prefix: str, suffix: str = ".tmp") -> str: ...

    @abstractmethod
    async def remove_file(self, path: str) -> None: ...

    @abstractmethod
    async def abort(self) -> None: ...

    @abstractmethod
    async def cleanup(self) -> None: ...


def _trimmed(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\0", "\n", "\r"))
    ):
        raise InvalidSDKRequestError(f"{name} must be non-empty trimmed text without control characters")
    return value


def attach_workspace(
    *,
    container_id: str,
    repo_root: str,
    command_prefix: Callable[[str], str] | str | None = None,
    timeout_returncode: int = -1,
) -> ExecutionEnvironment:
    """Attach to a caller-owned container by name or full immutable ID."""
    container_id = _trimmed(container_id, "container_id")
    repo_root = _trimmed(repo_root, "repo_root")
    if not posixpath.isabs(repo_root) or posixpath.normpath(repo_root) != repo_root:
        raise InvalidSDKRequestError("repo_root must be a normalized absolute container path")
    if isinstance(timeout_returncode, bool) or not isinstance(timeout_returncode, int):
        raise InvalidSDKRequestError("timeout_returncode must be an integer")
    from opencollab.adapters.env import DockerWorkspaceEnvironment

    return DockerWorkspaceEnvironment(
        container_id=container_id,
        repo_root=repo_root,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


__all__ = ["CommandResult", "ExecResult", "ExecutionEnvironment", "attach_workspace"]
