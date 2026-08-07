"""Composition layer for CLI and SDK wiring around application and domain code."""

from opencollab.bootstrap.agent_runtime import (
    AgentRuntimeLifecycleError,
    AgentRuntimeResult,
    run_agent,
)
from opencollab.bootstrap.config import OpenCollabConfig, build_config, get_config
from opencollab.bootstrap.container import (
    DefaultSessionFactory,
    RuntimeContext,
    SessionRuntime,
    SnapshotSessionError,
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
    "AgentRuntimeLifecycleError",
    "AgentRuntimeResult",
    "DefaultSessionFactory",
    "OpenCollabConfig",
    "RuntimeContext",
    "SessionRuntime",
    "SnapshotSessionError",
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
    "run_agent",
    "snapshot_session",
]
