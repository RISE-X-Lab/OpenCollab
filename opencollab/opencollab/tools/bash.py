"""BashTool — terminal execution with safety and output truncation.

Ref:
- kimi-cli: Bash tool with runtime approval
- opencode: bash tool with permission checking
- openclaw: exec-safety.ts with sandbox context
- User feedback: stdout/stderr truncation is critical (npm install can produce 100k chars)
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from opencollab.application.ports import EnvironmentPort, SafetyPolicyPort
from opencollab.tools.base import Tool

# Max chars to keep from stdout/stderr (ref: user feedback blind spot #1)
MAX_OUTPUT_CHARS = 8_000


class BashTool(Tool):
    """Execute a shell command in the workspace.

    Output is automatically truncated to prevent context explosion.
    Risky commands require human confirmation (unless in yolo mode).
    Destructive commands are hard-blocked.
    """

    name = "bash"
    description = (
        "Execute a shell command in the workspace. Returns stdout and stderr. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 120).",
            },
        },
        "required": ["command"],
    }

    async def execute(
        self,
        params: dict[str, Any],
        env: EnvironmentPort | None = None,
        interceptor: SafetyPolicyPort | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> str:
        cmd = params["command"]
        timeout = params.get("timeout", 120.0)

        if not env:
            return "Error: no execution environment available."

        # Safety checks
        if interceptor:
            await interceptor.check_cmd_interactive(cmd, confirm_fn)

        result = await env.exec_cmd(cmd, timeout=timeout)

        # Format output with truncation (ref: user blind spot #1)
        stdout = _truncate(result.stdout, MAX_OUTPUT_CHARS, "stdout")
        stderr = _truncate(result.stderr, MAX_OUTPUT_CHARS, "stderr")

        parts = [f"Exit code: {result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)


def _truncate(text: str, max_chars: int, label: str) -> str:
    """Keep head + tail, drop middle to avoid context explosion."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... [{len(text) - max_chars} chars of {label} truncated] ...\n\n"
        + text[-half:]
    )
