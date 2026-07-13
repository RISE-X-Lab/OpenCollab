"""Stable environment contracts and concrete execution adapters."""

from __future__ import annotations

from opencollab.adapters.env import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    DockerEnvironment,
    LocalEnvironment,
    WorktreeEnvironment,
)

from .environment import ExecResult, ExecutionEnvironment

__all__ = [
    "PROCESS_OUTPUT_CAPTURE_BYTES",
    "DockerEnvironment",
    "ExecResult",
    "ExecutionEnvironment",
    "LocalEnvironment",
    "WorktreeEnvironment",
]
