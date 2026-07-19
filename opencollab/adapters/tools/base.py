"""Tool base class — JSON Schema driven, async execution.

First Principle: A Tool is a function with a JSON Schema input and string output.
LLM calls tools via function calling; the framework routes to the right
Tool.execute_with_runtime().

Ref:
- kimi-cli: CallableTool2[Params] with Pydantic schema + async __call__
- opencode: ToolRegistry with init/execute pattern
- openclaw: AnyAgentTool structural interface
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from filelock import FileLock, Timeout

from opencollab.application.tool_execution import DeferredCall, ToolRuntime

HOST_WRITE_LOCK_TIMEOUT_SECONDS = 10.0
HOST_WRITE_LOCK_POLL_SECONDS = 0.02


def _host_lock_root() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = os.path.join(tempfile.gettempdir(), f"opencollab-write-locks-{uid}")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _host_lock_path(path: str, env: Any) -> str:
    workspace = getattr(env, "workspace", "") if env is not None else ""
    identity = os.path.abspath(os.path.join(workspace, path)) if workspace else os.path.abspath(path)
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    return os.path.join(_host_lock_root(), f"{digest}.lock")


@contextlib.asynccontextmanager
async def host_write_lock(path: str, env: Any) -> AsyncIterator[Any]:
    """Acquire a cross-process host lock without blocking the asyncio loop."""
    if env is not None and not getattr(env, "local_filesystem", False):
        yield None
        return

    lock = FileLock(_host_lock_path(path, env), timeout=0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HOST_WRITE_LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        while True:
            try:
                lock.acquire(timeout=0)
                acquired = True
                break
            except Timeout:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"timed out acquiring write lock for {path}")
                await asyncio.sleep(min(HOST_WRITE_LOCK_POLL_SECONDS, remaining))
        yield lock
    finally:
        if acquired:
            lock.release()


class Tool:
    """Base class for all tools. Subclass and implement ``execute_with_runtime``.

    Attributes:
        name: Tool name as seen by the LLM (snake_case recommended).
        description: One-line description for the LLM to understand when to use it.
        parameters: JSON Schema dict describing the input parameters.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    default_timeout: float | None = None
    disable_outer_timeout: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str | DeferredCall:
        """Execute the tool with the runtime bundle.

        Returns the result as a string; a deferrable tool returns a
        ``DeferredCall`` instead when it hands work off.
        """
        raise NotImplementedError(
            f"Tool '{self.name}' must implement execute_with_runtime()"
        )
