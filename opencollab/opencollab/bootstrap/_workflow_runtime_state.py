"""Shared workflow-runtime limits and owned background-task registries."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)
DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS = 2.0
_LATE_TRACER_OWNER_TASKS: set[asyncio.Task[Any]] = set()
_LATE_TRACER_FAILURES: deque[BaseException] = deque(maxlen=64)
MAX_WORKFLOW_DIRECTORY_ENTRIES = 4_096
MAX_WORKFLOW_FILES = 256
MAX_WORKFLOW_SOURCE_BYTES = 4 * 1024 * 1024
_WORKFLOW_MANIFEST_OWNER_TASKS: set[asyncio.Task[Any]] = set()
