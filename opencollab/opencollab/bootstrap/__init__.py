"""Composition layer — CLI/eval wiring without touching core or domain internals."""

from opencollab.bootstrap.config import OpenCollabConfig, build_config, get_config
from opencollab.bootstrap.container import (
    DefaultSessionFactory,
    RuntimeContext,
    SessionRuntime,
    SpawnConfig,
    build_runtime_context,
    build_scheduler,
    build_session,
    build_session_runtime,
    build_spawn_session,
    build_workspace_safety_policy,
    load_session,
    snapshot_session,
)

__all__ = [
    "DefaultSessionFactory",
    "OpenCollabConfig",
    "RuntimeContext",
    "SessionRuntime",
    "SpawnConfig",
    "build_config",
    "build_runtime_context",
    "build_scheduler",
    "build_session",
    "build_session_runtime",
    "build_spawn_session",
    "build_workspace_safety_policy",
    "get_config",
    "load_session",
    "snapshot_session",
]
