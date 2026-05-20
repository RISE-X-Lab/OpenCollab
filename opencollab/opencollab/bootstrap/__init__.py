"""Composition layer — CLI/eval wiring without touching core or team internals."""

from opencollab.bootstrap.config import OpenCollabConfig, build_config, get_config
from opencollab.bootstrap.container import (
    DefaultSessionFactory,
    RuntimeContext,
    SessionRuntime,
    TeammateConfig,
    build_chat_session,
    build_default_tools,
    build_runtime_context,
    build_session,
    build_session_runtime,
    build_team,
    build_teammate_session,
    build_workspace_safety_policy,
    load_session,
    snapshot_session,
)

__all__ = [
    "DefaultSessionFactory",
    "OpenCollabConfig",
    "RuntimeContext",
    "SessionRuntime",
    "TeammateConfig",
    "build_chat_session",
    "build_config",
    "build_default_tools",
    "build_runtime_context",
    "build_session",
    "build_session_runtime",
    "build_team",
    "build_teammate_session",
    "build_workspace_safety_policy",
    "get_config",
    "load_session",
    "snapshot_session",
]
