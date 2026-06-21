"""File system tools — read, write (str_replace_editor), grep.

Only 3 tools — the minimal set every coding agent needs.

Ref:
- kimi-cli: ReadFile with line_offset/n_lines, WriteFile with str_replace
- opencode: read, edit (str_replace), write, glob, grep tools
- claude-code: Read, Edit (str_replace), Grep, Glob
- openclaw: wrapToolWorkspaceRootGuard for path safety
"""

from __future__ import annotations

import shlex
from typing import Any

from opencollab.adapters.tools._output import truncate
from opencollab.adapters.tools.base import Tool, host_write_lock
from opencollab.application.tool_execution import ToolRuntime

# Line limits alone don't bound the context cost — one minified/long-line file
# can dwarf 500 normal lines. Char caps are the hard backstop (ref: bash.py).
MAX_READ_CHARS = 30_000
MAX_GREP_CHARS = 10_000

# create-mode guard: refuse to silently replace a substantial file with a much
# smaller one — the classic "model rewrote a truncated copy" failure.
OVERWRITE_GUARD_MIN_CHARS = 1_000
OVERWRITE_GUARD_SHRINK_RATIO = 0.5


class FileReadTool(Tool):
    """Read file content with optional line range."""

    name = "file_read"
    description = (
        "Read a file's content. Supports reading specific line ranges. "
        "Returns content with line numbers. "
        "On large files, prefer a ranged read (offset/limit) over reading the whole file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to workspace."},
            "offset": {
                "type": "integer",
                "description": "Start line (1-based). Pair with limit to read a "
                "slice — e.g. the lines around a grep hit.",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read (default 500). For large files "
                "prefer a small window (~60-120) over the full default.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, max_read_chars: int = MAX_READ_CHARS):
        self.max_read_chars = max_read_chars

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

        if not env:
            return "Error: no execution environment available."

        if safety_policy:
            path = safety_policy.check_path(path)

        try:
            content = await env.read_file(path)
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
        body = header + "\n" + truncate("\n".join(numbered), self.max_read_chars)
        # Loud footer when lines remain below the shown range — otherwise a
        # default read silently stops at the limit and the tail is lost.
        if end < total:
            body += (
                f"\n... {total - end} more lines below (showing {start + 1}-{end} "
                f"of {total}). Continue with offset={end + 1}, or use the grep "
                f"tool to jump to a symbol."
            )
        return body


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
        "Use str_replace for targeted edits (most common). Use create for new files. "
        "Read the target span with file_read first; replacements apply to the file's "
        "current on-disk content."
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
            "overwrite": {
                "type": "boolean",
                "description": "For 'create' mode: set true to confirm replacing an "
                "existing file with much shorter content (guarded otherwise).",
            },
        },
        "required": ["path", "mode"],
    }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        path = params["path"]
        mode = params["mode"]
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if not env:
            return "Error: no execution environment available."

        if safety_policy:
            path = safety_policy.check_path(path)

        try:
            with host_write_lock(path, env):
                if mode == "create":
                    return await self._create(
                        env,
                        path,
                        params.get("content", ""),
                        overwrite=params.get("overwrite", False),
                    )
                if mode == "str_replace":
                    return await self._str_replace(env, path, params)
                return f"Error: unknown mode '{mode}'. Use 'create' or 'str_replace'."
        except PermissionError as e:
            return f"Error: {e}"

    async def _create(
        self, env: Any, path: str, content: str, *, overwrite: bool = False
    ) -> str:
        """Write ``content`` to ``path``, creating parent directories as needed.

        Guard: replacing a substantial existing file with much shorter content
        is refused unless ``overwrite`` is set — that shape is almost always a
        model accidentally writing a truncated copy, not an intentional rewrite.
        """
        if not overwrite:
            try:
                current = await env.read_file(path)
            except FileNotFoundError:
                current = None
            if (
                current is not None
                and len(current) >= OVERWRITE_GUARD_MIN_CHARS
                and len(content) < len(current) * OVERWRITE_GUARD_SHRINK_RATIO
            ):
                return (
                    f"Error: refusing to overwrite {path} ({len(current)} chars) "
                    f"with much shorter content ({len(content)} chars). For "
                    "targeted edits use str_replace or the apply_patch tool; to "
                    "intentionally replace the whole file, retry with "
                    "overwrite: true."
                )
        await env.write_file(path, content)
        return f"Created/wrote {path} ({len(content)} chars)"

    async def _str_replace(self, env: Any, path: str, params: dict[str, Any]) -> str:
        """Replace a unique occurrence of ``old_str`` with ``new_str`` in ``path``."""
        old_str = params.get("old_str", "")
        new_str = params.get("new_str", "")
        if not old_str:
            return "Error: old_str is required for str_replace mode."

        current = await env.read_file(path)

        # Check uniqueness (ref: claude-code Edit — must be unique)
        count = current.count(old_str)
        if count == 0:
            return (
                f"Error: old_str not found in {path}. Make sure the text matches "
                "exactly (including whitespace). If the edit keeps failing to "
                "match, use the apply_patch tool instead."
            )
        if count > 1:
            return f"Error: old_str found {count} times in {path}. Provide more context to make it unique."

        updated = current.replace(old_str, new_str, 1)
        await env.write_file(path, updated)
        return f"Replaced in {path}: {len(old_str)} chars → {len(new_str)} chars"


class GrepTool(Tool):
    """Search file contents using regex patterns."""

    name = "grep"
    description = (
        "Search files with a regex to locate symbols/strings/refs. Returns "
        "file:line:match. Prefer this over bash grep/rg/find — it pinpoints lines "
        "without pulling whole files into context; then file_read around the hit. "
        "Use glob to filter file types, path to narrow scope, max_results to cap "
        "output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {
                "type": "string",
                "description": "Directory/file to search under (default: workspace "
                "root). Narrow it to speed up large repos.",
            },
            "glob": {
                "type": "string",
                "description": "File glob to filter (e.g. '*.py'). Omit to search "
                "all files.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max matching lines (default 50). Raise to widen the "
                "net, lower if output floods context.",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, max_grep_chars: int = MAX_GREP_CHARS):
        self.max_grep_chars = max_grep_chars

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

        if not env:
            return "Error: no execution environment available."

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
            return truncate(result.stdout.strip(), self.max_grep_chars)
        return f"No matches found for pattern: {pattern}"
