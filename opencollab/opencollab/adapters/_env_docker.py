"""Concrete Docker environment assembled from focused capability mixins."""

from __future__ import annotations

from opencollab.adapters._env_base import Environment
from opencollab.adapters._env_docker_exec import DockerExecMixin
from opencollab.adapters._env_docker_files import DockerFilesMixin
from opencollab.adapters._env_docker_lifecycle import DockerLifecycleMixin
from opencollab.adapters._env_docker_teardown import DockerTeardownMixin


class DockerEnvironment(
    DockerLifecycleMixin,
    DockerExecMixin,
    DockerFilesMixin,
    DockerTeardownMixin,
    Environment,
):
    """Docker container sandbox for evaluation and SWE-bench execution."""

    process_isolated = True
