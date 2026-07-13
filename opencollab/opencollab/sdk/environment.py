"""Stable execution-environment factories for external integrations."""

from __future__ import annotations

import posixpath
from collections.abc import Callable

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters.env import DockerWorkspaceEnvironment

from .errors import InvalidSDKRequestError

ExecutionEnvironment = Environment


def attach_workspace(
    *,
    container_id: str,
    repo_root: str,
    command_prefix: Callable[[str], str] | str | None = None,
    timeout_returncode: int = -1,
) -> Environment:
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
    return DockerWorkspaceEnvironment(
        container_id=container_id,
        repo_root=repo_root,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


__all__ = ["ExecResult", "ExecutionEnvironment", "attach_workspace"]
