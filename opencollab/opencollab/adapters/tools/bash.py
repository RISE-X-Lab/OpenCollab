"""BashTool — terminal execution with safety and output truncation.

Ref:
- kimi-cli: Bash tool with runtime approval
- opencode: bash tool with permission checking
- openclaw: exec-safety.ts with sandbox context
- User feedback: stdout/stderr truncation is critical (npm install can produce 100k chars)
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools._output import truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

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
        "Use this for installing packages, build/setup commands, and one-off "
        "inspection. Prefer the dedicated tool when one exists: `run_tests` to "
        "run tests (structured result), `git_diff` to view uncommitted changes, "
        "`grep` to search file contents, `file_read`/`file_write` to read or "
        "edit files. "
        "Runs non-interactively with no TTY: pass non-interactive flags (e.g. -y), "
        "pipe pagers to cat, and never block on prompts. The working directory does "
        "NOT persist between calls — use absolute paths rather than cd."
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

    def __init__(self, max_output_chars: int = MAX_OUTPUT_CHARS):
        self.max_output_chars = max_output_chars

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        cmd = params["command"]
        timeout = params.get("timeout", 120.0)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if not env:
            return "Error: no execution environment available."

        # Safety checks
        if safety_policy:
            await safety_policy.check_cmd_interactive(cmd, runtime.confirm_fn())

        result = await env.exec_cmd(cmd, timeout=timeout)

        # Format output with truncation (ref: user blind spot #1)
        stdout = truncate(result.stdout, self.max_output_chars, "stdout")
        stderr = truncate(result.stderr, self.max_output_chars, "stderr")

        parts = [f"Exit code: {result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)
