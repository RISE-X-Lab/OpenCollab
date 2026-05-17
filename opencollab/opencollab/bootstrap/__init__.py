"""Composition layer — CLI/eval wiring without touching core or team internals."""

from opencollab.bootstrap.runtime import RuntimeContext, build_runtime_context
from opencollab.bootstrap.session_factory import build_chat_session, build_team
from opencollab.bootstrap.tool_factory import build_default_tools

__all__ = [
    "RuntimeContext",
    "build_runtime_context",
    "build_default_tools",
    "build_chat_session",
    "build_team",
]
