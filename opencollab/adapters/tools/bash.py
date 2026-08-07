"""BashTool — terminal execution with safety and output truncation.

Ref:
- kimi-cli: Bash tool with runtime approval
- opencode: bash tool with permission checking
- openclaw: exec-safety.ts with sandbox context
- User feedback: stdout/stderr truncation is critical (npm install can produce 100k chars)
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools._output import require_positive_int, truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

# Max chars to keep from stdout/stderr (ref: user feedback blind spot #1)
MAX_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT = 120.0


def _format_captured_stream(
    text: str,
    max_chars: int,
    label: str,
    dropped_bytes: int,
) -> str:
    if dropped_bytes <= 0:
        return truncate(text, max_chars, label)
    marker = (
        f"\n\n... [{dropped_bytes} bytes of {label} dropped during capture] ...\n\n"
    )
    if len(marker) >= max_chars:
        return marker[:max_chars]
    source_budget = max_chars - len(marker)
    head_budget = (source_budget + 1) // 2
    tail_budget = source_budget - head_budget
    captured_split = (len(text) + 1) // 2
    captured_head = text[:captured_split]
    captured_tail = text[captured_split:]
    tail = captured_tail[-tail_budget:] if tail_budget else ""
    return captured_head[:head_budget] + marker + tail


class BashTool(Tool):
    """Execute a shell command in the workspace.

    Output is automatically truncated to prevent context explosion.
    Risky commands require human confirmation (unless in yolo mode).
    Destructive commands are hard-blocked.
    """

    name = "bash"
    default_timeout = DEFAULT_TIMEOUT
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
                "description": "Timeout in seconds (default 120). Raise for slow "
                "installs/builds or the full test suite.",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        *,
        require_process_isolation: bool = False,
    ):
        self.max_output_chars = require_positive_int(
            max_output_chars, "max_output_chars"
        )
        self.require_process_isolation = require_process_isolation

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        cmd = params["command"]
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if env is None:
            return "Error: no execution environment available."
        if self.require_process_isolation and not getattr(
            env, "process_isolated", False
        ):
            return (
                "Error: bash is disabled because this execution environment "
                "does not provide an OS process sandbox."
            )

        # Safety checks
        if safety_policy:
            await safety_policy.check_cmd_interactive(cmd, runtime.confirm_fn())

        result = await env.exec_cmd(cmd, timeout=timeout)

        # Format output with truncation (ref: user blind spot #1)
        stdout = _format_captured_stream(
            result.stdout,
            self.max_output_chars,
            "stdout",
            getattr(result, "stdout_dropped_bytes", 0),
        )
        stderr = _format_captured_stream(
            result.stderr,
            self.max_output_chars,
            "stderr",
            getattr(result, "stderr_dropped_bytes", 0),
        )

        parts = [f"Exit code: {result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)
