"""Check workspace paths for filesystem tools."""

from __future__ import annotations

import os
import posixpath

from opencollab.application.tool_execution import ToolRuntime


def checked_path(runtime: ToolRuntime, path: str, *, native_file: bool = False) -> str:
    """Apply the runtime policy and preserve remote components for native file operations."""
    policy = runtime.safety_policy
    remote_check = getattr(policy, "check_remote_file_path", None)
    if native_file and callable(remote_check):
        remote_path = remote_check(path)
        if remote_path is not None:
            return remote_path
    checked = policy.check_path(path) if policy else path
    environment = runtime.environment
    workspace = getattr(environment, "workspace", None)
    if policy and workspace is not None and not getattr(environment, "local_filesystem", False):
        # Keep a remote workspace in its own namespace after host policy validation.
        host_root = os.path.realpath(workspace)
        if os.path.isabs(checked) and os.path.commonpath((host_root, checked)) == host_root:
            checked = posixpath.join(workspace, os.path.relpath(checked, host_root))
    return checked
