"""Stable environment contracts and concrete execution adapters."""

from __future__ import annotations

from opencollab.adapters.env import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    DockerEnvironment,
    LocalEnvironment,
    WorktreeEnvironment,
)

from .environment import CommandResult, ExecResult, ExecutionEnvironment, attach_workspace

__all__ = [
    "PROCESS_OUTPUT_CAPTURE_BYTES",
    "CommandResult",
    "DockerEnvironment",
    "ExecResult",
    "ExecutionEnvironment",
    "LocalEnvironment",
    "WorktreeEnvironment",
    "attach_workspace",
]
