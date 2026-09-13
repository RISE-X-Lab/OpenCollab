"""Public execution-environment contract and thin composition helpers."""

from opencollab.application.ports import EnvironmentPort as Environment
from opencollab.bootstrap.programmatic import (
    attach_container,
    build_repo_map_via_env,
    docker_environment,
    local_environment,
    worktree_environment,
)

__all__ = [
    "Environment",
    "attach_container",
    "build_repo_map_via_env",
    "docker_environment",
    "local_environment",
    "worktree_environment",
]
