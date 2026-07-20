"""Public execution-environment adapters."""

from __future__ import annotations

from collections.abc import Callable

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import PROCESS_OUTPUT_CAPTURE_BYTES
from opencollab.adapters._env_worktree import WorktreeEnvironment


class DockerWorkspaceEnvironment(DockerEnvironment):
    """Attach tools to an already-running task container workspace."""

    def __init__(
        self,
        *,
        container_id: str,
        repo_root: str,
        command_prefix: Callable[[str], str] | str | None = None,
        timeout_returncode: int = -1,
    ) -> None:
        super().__init__(
            workspace=repo_root,
            container_id=container_id,
            exec_workdir=repo_root,
            command_prefix=command_prefix,
            timeout_returncode=timeout_returncode,
        )


__all__ = [
    "DockerEnvironment",
    "DockerWorkspaceEnvironment",
    "Environment",
    "ExecResult",
    "LocalEnvironment",
    "PROCESS_OUTPUT_CAPTURE_BYTES",
    "WorktreeEnvironment",
]
