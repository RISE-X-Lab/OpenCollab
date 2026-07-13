"""Stable execution-environment contracts and attachment factories."""

from __future__ import annotations

import posixpath
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import InvalidSDKRequestError


@runtime_checkable
class CommandResult(Protocol):
    """Structural result contract returned by environment commands."""

    @property
    @abstractmethod
    def returncode(self) -> int: ...

    @property
    @abstractmethod
    def stdout(self) -> str: ...

    @property
    @abstractmethod
    def stderr(self) -> str: ...

    @property
    @abstractmethod
    def stdout_truncated(self) -> bool: ...

    @property
    @abstractmethod
    def stderr_truncated(self) -> bool: ...

    @property
    @abstractmethod
    def stdout_dropped_bytes(self) -> int: ...

    @property
    @abstractmethod
    def stderr_dropped_bytes(self) -> int: ...


@dataclass(slots=True)
class ExecResult:
    """SDK-owned command result value for external environment adapters."""

    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


@runtime_checkable
class ExecutionEnvironment(Protocol):
    """Structural environment contract accepted by the public runtime.

    External integrations may implement this protocol directly. They do not
    need to inherit from an OpenCollab adapter class.
    """

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
    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> CommandResult: ...

    @abstractmethod
    async def read_file(self, path: str) -> str: ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str: ...

    @abstractmethod
    async def remove_file(self, path: str) -> None: ...

    @abstractmethod
    async def registered_retirement_paths(self) -> tuple[str, ...]: ...

    @abstractmethod
    async def abort(self) -> None: ...

    @abstractmethod
    async def cleanup(self) -> None: ...


def attach_workspace(
    *,
    container_id: str,
    repo_root: str,
    command_prefix: Callable[[str], str] | str | None = None,
    timeout_returncode: int = -1,
) -> ExecutionEnvironment:
    """Attach OpenCollab to an existing container without owning its lifecycle.

    The caller remains responsible for creating, validating, and ultimately
    removing the container. OpenCollab owns the commands it starts inside it.
    """
    if not isinstance(container_id, str) or not container_id or container_id != container_id.strip():
        raise InvalidSDKRequestError("container_id must be a non-empty, trimmed string")
    if "\x00" in container_id or "\n" in container_id or "\r" in container_id:
        raise InvalidSDKRequestError("container_id contains invalid control characters")
    if not isinstance(repo_root, str) or not repo_root or repo_root != repo_root.strip():
        raise InvalidSDKRequestError("repo_root must be a non-empty, trimmed string")
    if "\x00" in repo_root or "\n" in repo_root or "\r" in repo_root:
        raise InvalidSDKRequestError("repo_root contains invalid control characters")
    if not posixpath.isabs(repo_root):
        raise InvalidSDKRequestError("repo_root must be an absolute container path")
    normalized_root = posixpath.normpath(repo_root)
    if normalized_root != repo_root:
        raise InvalidSDKRequestError("repo_root must be a normalized container path")
    if not isinstance(timeout_returncode, int) or isinstance(timeout_returncode, bool):
        raise InvalidSDKRequestError("timeout_returncode must be an integer")
    # Imported lazily so the protocol and command result remain usable without
    # importing any concrete adapter implementation.
    from opencollab.adapters.env import DockerWorkspaceEnvironment

    return DockerWorkspaceEnvironment(
        container_id=container_id,
        repo_root=repo_root,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


__all__ = [
    "CommandResult",
    "ExecResult",
    "ExecutionEnvironment",
    "attach_workspace",
]
