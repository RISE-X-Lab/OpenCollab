"""Execution-environment compatibility facade.

Concrete capabilities live in focused sibling modules. Existing imports and
runtime monkeypatches continue to target this module.
"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from opencollab.adapters import (
    _env_config,
    _env_docker_exec,
    _env_docker_files,
    _env_docker_lifecycle,
    _env_docker_teardown,
    _env_file_io,
    _env_local,
    _env_process,
    _env_worktree,
    _env_worktree_directory,
    _env_worktree_lifecycle,
)
from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import (
    _DOCKER_ATTACH_INSPECT_FORMAT,
    _DOCKER_CREATE_WRITE_AND_VERIFY,
    _DOCKER_EXEC_CANCEL,
    _DOCKER_EXEC_WRAPPER,
    _DOCKER_INSPECT_FORMAT,
    _DOCKER_MISSING_RE,
    _DOCKER_REMOVE_OWNED_TEMP,
    _DOCKER_WRITE_AND_VERIFY,
    _PROCESS_POPEN,
    DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS,
    DOCKER_COMPENSATION_TIMEOUT_SECONDS,
    DOCKER_CONTAINER_NAME_MAX_BYTES,
    DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE,
    DOCKER_OWNER_LABEL,
    DOCKER_REFERENCE_MAX_BYTES,
    DOCKER_SETUP_TIMEOUT_SECONDS,
    DOCKER_WRITE_TIMEOUT_SECONDS,
    LOCAL_FILE_READ_LIMIT_BYTES,
    LOCAL_FILE_WRITE_LIMIT_BYTES,
    PROCESS_IO_JOIN_TIMEOUT_SECONDS,
    PROCESS_KILL_REAP_TIMEOUT_SECONDS,
    PROCESS_OUTPUT_CAPTURE_BYTES,
    PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
    PROCESS_TERM_GRACE_SECONDS,
    WORKTREE_GIT_TIMEOUT_SECONDS,
    _validate_docker_container_reference,
    _validate_docker_image_reference,
)
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_file_io import (
    _await_owned_transaction,
    _directory_open_flags,
    _lstat_for_nofollow_compat,
    _open_parent_dirfd,
    _open_regular_file_flags,
    _positive_file_size_limit,
    _positive_finite_timeout,
    _run_owned_blocking_io,
    _sync_create_temp_file,
    _sync_read_regular_file,
    _sync_unlink_file,
    _sync_write_regular_file,
    _verify_opened_regular_file,
    _verify_path_still_names_open_file,
)
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _BoundedCapture,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
    _sync_process_group_exists,
    _sync_run_cleanup_command,
    _sync_signal_process_group,
    _sync_terminate_process_group,
    _sync_wait_for_process_group_exit,
    _ThreadProcessOwner,
    _ThreadProcessResult,
    _wait_thread_event,
)
from opencollab.adapters._env_worktree import WorktreeEnvironment

logger = logging.getLogger(__name__)


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
            image="python:3.11-slim",
            workspace=repo_root,
            container_id=container_id,
            exec_workdir=repo_root,
            command_prefix=command_prefix,
            timeout_returncode=timeout_returncode,
        )


_COMPATIBILITY_MODULES = (
    _env_config,
    _env_file_io,
    _env_process,
    _env_local,
    _env_worktree,
    _env_worktree_directory,
    _env_worktree_lifecycle,
    _env_docker_lifecycle,
    _env_docker_exec,
    _env_docker_files,
    _env_docker_teardown,
)


class _EnvironmentFacade(types.ModuleType):
    """Mirror patched compatibility names into their implementation modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _EnvironmentFacade

__all__ = [
    "DockerEnvironment",
    "DockerWorkspaceEnvironment",
    "Environment",
    "ExecResult",
    "LocalEnvironment",
    "WorktreeEnvironment",
]
