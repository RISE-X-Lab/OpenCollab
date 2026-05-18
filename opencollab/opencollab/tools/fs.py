"""File system tools — read, write (str_replace_editor), grep.

Only 3 tools — the minimal set every coding agent needs.

Ref:
- kimi-cli: ReadFile with line_offset/n_lines, WriteFile with str_replace
- opencode: read, edit (str_replace), write, glob, grep tools
- claude-code: Read, Edit (str_replace), Grep, Glob
- openclaw: wrapToolWorkspaceRootGuard for path safety
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Callable, Awaitable

from filelock import FileLock

from opencollab.application.ports import EnvironmentPort, SafetyPolicyPort
from opencollab.application.tool_runtime import ToolRuntime, tool_runtime_from_legacy
from opencollab.tools.base import Tool


class FileReadTool(Tool):
    """Read file content with optional line range."""

    name = "file_read"
    description = (
        "Read a file's content. Supports reading specific line ranges. "
        "Returns content with line numbers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to workspace."},
            "offset": {"type": "integer", "description": "Starting line number (1-based, default 1)."},
            "limit": {"type": "integer", "description": "Number of lines to read (default 500)."},
        },
        "required": ["path"],
    }

    async def execute(
        self,
        params: dict[str, Any],
        env: EnvironmentPort | None = None,
        interceptor: SafetyPolicyPort | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> str:
        runtime = tool_runtime_from_legacy(
            env=env,
            interceptor=interceptor,
            confirm_fn=confirm_fn,
        )
        return await self.execute_with_runtime(params, runtime)

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        path = params["path"]
        offset = params.get("offset", 1)
        limit = params.get("limit", 500)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if safety_policy:
            path = safety_policy.check_path(path)

        try:
            if env:
                content = await env.read_file(path)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
        except FileNotFoundError:
            return f"Error: file not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"

        lines = content.splitlines()
        total = len(lines)

        # Apply line range
        start = max(0, offset - 1)
        end = min(total, start + limit)
        selected = lines[start:end]

        # Format with line numbers (ref: claude-code cat -n format)
        numbered = [f"{start + i + 1}\t{line}" for i, line in enumerate(selected)]
        header = f"File: {params['path']} ({total} lines total, showing {start + 1}-{end})"
        return header + "\n" + "\n".join(numbered)


class FileWriteTool(Tool):
    """Write to files using str_replace pattern (find-and-replace blocks).

    This is the most token-efficient way for LLMs to edit files — they only
    need to specify the old text and new text, not rewrite the entire file.

    Ref: opencode edit tool, claude-code Edit tool — str_replace is the standard.
    """

    name = "file_write"
    description = (
        "Edit a file. Modes:\n"
        "- 'create': Write a new file (or overwrite entirely).\n"
        "- 'str_replace': Find old_str and replace with new_str. Must be unique match.\n"
        "Use str_replace for targeted edits (most common). Use create for new files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to workspace."},
            "mode": {
                "type": "string",
                "enum": ["create", "str_replace"],
                "description": "Edit mode.",
            },
            "content": {
                "type": "string",
                "description": "Full file content (for 'create' mode).",
            },
            "old_str": {
                "type": "string",
                "description": "Text to find (for 'str_replace' mode). Must be unique.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text (for 'str_replace' mode).",
            },
        },
        "required": ["path", "mode"],
    }

    async def execute(
        self,
        params: dict[str, Any],
        env: EnvironmentPort | None = None,
        interceptor: SafetyPolicyPort | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> str:
        runtime = tool_runtime_from_legacy(
            env=env,
            interceptor=interceptor,
            confirm_fn=confirm_fn,
        )
        return await self.execute_with_runtime(params, runtime)

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        path = params["path"]
        mode = params["mode"]
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if safety_policy:
            path = safety_policy.check_path(path)

        # File lock for concurrent safety (ref: design doc filelock)
        lock = FileLock(f"{path}.lock", timeout=10)

        try:
            with lock:
                if mode == "create":
                    content = params.get("content", "")
                    if env:
                        await env.write_file(path, content)
                    else:
                        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(content)
                    return f"Created/wrote {path} ({len(content)} chars)"

                elif mode == "str_replace":
                    old_str = params.get("old_str", "")
                    new_str = params.get("new_str", "")
                    if not old_str:
                        return "Error: old_str is required for str_replace mode."

                    # Read current content
                    if env:
                        current = await env.read_file(path)
                    else:
                        with open(path, "r", encoding="utf-8") as f:
                            current = f.read()

                    # Check uniqueness (ref: claude-code Edit — must be unique)
                    count = current.count(old_str)
                    if count == 0:
                        return f"Error: old_str not found in {path}. Make sure the text matches exactly."
                    if count > 1:
                        return f"Error: old_str found {count} times in {path}. Provide more context to make it unique."

                    # Replace
                    updated = current.replace(old_str, new_str, 1)
                    if env:
                        await env.write_file(path, updated)
                    else:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(updated)
                    return f"Replaced in {path}: {len(old_str)} chars → {len(new_str)} chars"

                else:
                    return f"Error: unknown mode '{mode}'. Use 'create' or 'str_replace'."

        except PermissionError as e:
            return f"Error: {e}"


class GrepTool(Tool):
    """Search file contents using regex patterns."""

    name = "grep"
    description = (
        "Search for a regex pattern across files in the workspace. "
        "Returns matching lines with file paths and line numbers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "description": "Directory or file to search (default: workspace root)."},
            "glob": {"type": "string", "description": "File glob pattern to filter (e.g., '*.py')."},
            "max_results": {"type": "integer", "description": "Max matching lines to return (default 50)."},
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        params: dict[str, Any],
        env: EnvironmentPort | None = None,
        interceptor: SafetyPolicyPort | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> str:
        runtime = tool_runtime_from_legacy(
            env=env,
            interceptor=interceptor,
            confirm_fn=confirm_fn,
        )
        return await self.execute_with_runtime(params, runtime)

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        pattern = params["pattern"]
        search_path = params.get("path", ".")
        glob_pattern = params.get("glob")
        max_results = params.get("max_results", 50)
        env = runtime.environment

        # Try using ripgrep if available (faster), fallback to Python
        if env:
            quoted_pattern = shlex.quote(pattern)
            quoted_search_path = shlex.quote(search_path)
            rg_cmd = f"rg -n --max-count {max_results} "
            if glob_pattern:
                rg_cmd += f"-g {shlex.quote(glob_pattern)} "
            rg_cmd += f"{quoted_pattern} {quoted_search_path} 2>/dev/null || grep -rn "
            if glob_pattern:
                rg_cmd += f"--include={shlex.quote(glob_pattern)} "
            rg_cmd += f"{quoted_pattern} {quoted_search_path} 2>/dev/null | head -n {max_results}"

            result = await env.exec_cmd(rg_cmd, timeout=30)
            if result.stdout.strip():
                return result.stdout.strip()
            return f"No matches found for pattern: {pattern}"

        # Pure Python fallback
        return self._python_grep(pattern, search_path, glob_pattern, max_results)

    def _python_grep(
        self, pattern: str, search_path: str, glob_pattern: str | None, max_results: int
    ) -> str:
        import fnmatch

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex: {e}"

        results = []
        for root, _dirs, files in os.walk(search_path):
            for fname in files:
                if glob_pattern and not fnmatch.fnmatch(fname, glob_pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append(f"{fpath}:{lineno}:{line.rstrip()}")
                                if len(results) >= max_results:
                                    return "\n".join(results)
                except (PermissionError, IsADirectoryError, OSError):
                    continue

        return "\n".join(results) if results else f"No matches found for pattern: {pattern}"
