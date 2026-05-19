"""Composition layer — CLI/eval wiring without touching core or team internals."""

from opencollab.bootstrap.config import OpenCollabConfig, build_config, get_config
from opencollab.bootstrap.runtime import RuntimeContext, build_runtime_context
from opencollab.bootstrap.session_factory import build_chat_session, build_team
from opencollab.bootstrap.tool_factory import build_default_tools

__all__ = [
    "OpenCollabConfig",
    "RuntimeContext",
    "build_config",
    "build_runtime_context",
    "build_default_tools",
    "build_chat_session",
    "build_team",
    "get_config",
]
