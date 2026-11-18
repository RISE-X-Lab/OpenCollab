"""Check workspace paths for filesystem tools."""

from __future__ import annotations

import os
import posixpath

from opencollab.application.tool_execution import ToolRuntime


def _checked_remote_path(runtime: ToolRuntime, path: str) -> str | None:
    check = getattr(runtime.safety_policy, "check_remote_file_path", None)
    return check(path) if callable(check) else None


def checked_path(runtime: ToolRuntime, path: str, *, native_file: bool = False) -> str:
    """Apply the runtime policy and preserve remote components for native file operations."""
    if native_file:
        remote_path = _checked_remote_path(runtime, path)
        if remote_path is not None:
            return remote_path
    policy = runtime.safety_policy
    checked = policy.check_path(path) if policy else path
    environment = runtime.environment
    workspace = getattr(environment, "workspace", None)
    if policy and workspace is not None and not getattr(environment, "local_filesystem", False):
        # Keep a remote workspace in its own namespace after host policy validation.
        host_root = os.path.realpath(workspace)
        if os.path.isabs(checked) and os.path.commonpath((host_root, checked)) == host_root:
            checked = posixpath.join(workspace, os.path.relpath(checked, host_root))
    return checked


async def checked_search_path(runtime: ToolRuntime, path: str) -> str:
    resolve = getattr(runtime.environment, "resolve_search_path", None)
    if callable(resolve):
        remote_path = _checked_remote_path(runtime, path)
        if remote_path is not None:
            return await resolve(remote_path)
    return checked_path(runtime, path)
