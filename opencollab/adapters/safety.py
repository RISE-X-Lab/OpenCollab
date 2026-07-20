"""SandboxInterceptor — the boundary between agents and the physical world.

Every tool call that touches the filesystem or terminal passes through here.
Provides:
1. Path jail — prevent workspace escape
2. Command filter — block destructive commands
3. Human-in-the-loop — confirm risky operations

Ref:
- openclaw: path-safety.ts, exec-safety.ts, boundary-path.ts
- Design doc: SandboxInterceptor with check_path/check_cmd
"""

from __future__ import annotations

import os
import re
from typing import Awaitable, Callable

# Commands that warrant human confirmation (not outright blocked)
RISKY_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*r|-[a-zA-Z]*f)", r"\bgit\s+push\b.*--force",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bchmod\s+777\b", r"\bsudo\b", r"\bcurl\b.*\|\s*(ba)?sh",
    r"\bwget\b.*\|\s*(ba)?sh", r"\bdd\s+", r"\bmkfs\b",
    r"\b:(){ :\|:& };:\b",  # fork bomb
]

# Commands that are always blocked
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/\s*$", r"\brm\s+-rf\s+/\*", r"\bmkfs\s+/dev/",
    r"\b>\s*/dev/sd", r"\bchmod\s+-R\s+777\s+/\s*$",
]

RISKY_RE = [re.compile(p) for p in RISKY_PATTERNS]
BLOCKED_RE = [re.compile(p) for p in BLOCKED_PATTERNS]


class SandboxInterceptor:
    """Transparent safety layer for all environment operations.

    Usage:
        interceptor = SandboxInterceptor("/path/to/workspace")
        safe_path = interceptor.check_path("../../../etc/passwd")  # raises
        interceptor.check_cmd("rm -rf /")  # raises
        await interceptor.check_cmd_interactive("rm -rf ./build", confirm_fn)  # asks user
    """

    def __init__(self, workspace_root: str, allowed_dirs: list[str] | None = None):
        self.root = os.path.abspath(workspace_root)
        # Additional allowed directories (e.g., /tmp for scratch)
        self.allowed_roots = [self.root] + [os.path.abspath(d) for d in (allowed_dirs or [])]

    def check_path(self, target_path: str) -> str:
        """Resolve and validate a file path. Returns absolute safe path or raises.

        Ref: openclaw boundary-path.ts — all paths must resolve within workspace.
        """
        # Resolve relative to workspace
        if not os.path.isabs(target_path):
            resolved = os.path.abspath(os.path.join(self.root, target_path))
        else:
            resolved = os.path.abspath(target_path)
        resolved = os.path.realpath(resolved)

        # Check against all allowed roots
        for allowed in self.allowed_roots:
            allowed_real = os.path.realpath(allowed)
            try:
                if os.path.commonpath([resolved, allowed_real]) == allowed_real:
                    return resolved
            except ValueError:
                continue

        raise PermissionError(
            f"Path escapes workspace: '{target_path}' resolved to '{resolved}' "
            f"(allowed roots: {self.allowed_roots})"
        )

    def check_cmd(self, cmd: str) -> None:
        """Hard-block destructive commands. Raises PermissionError."""
        for pattern in BLOCKED_RE:
            if pattern.search(cmd):
                raise PermissionError(f"Blocked destructive command: {cmd[:200]}")

    def is_risky(self, cmd: str) -> bool:
        """Check if command needs human confirmation."""
        for pattern in RISKY_RE:
            if pattern.search(cmd):
                return True
        return False

    async def check_cmd_interactive(
        self,
        cmd: str,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        """Block destructive commands, and ask confirmation for risky ones.

        If no confirm_fn provided, risky commands are auto-allowed (yolo mode).
        """
        self.check_cmd(cmd)  # Hard block first

        if self.is_risky(cmd) and confirm_fn:
            allowed = await confirm_fn(f"Risky command detected: {cmd}\nAllow execution?")
            if not allowed:
                raise PermissionError(f"User denied risky command: {cmd[:200]}")
