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
from opencollab.adapters.tools._paths import checked_path
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
        "On large files, prefer a ranged read (offset/limit) over reading the whole file. "
        "Distill as you read: after a read, write one line of what you learned before "
        "your next tool call — never send a tool call with an empty reply. Old reads are "
        "later cleared to a stub naming what you already read (e.g. "
        "'[...file_read foo.py lines 1-100...]'); trust that stub plus your own notes and "
        "do NOT re-read the same range to reconfirm what you already saw."
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

        if not env:
            return "Error: no execution environment available."

        path = checked_path(runtime, path)

        try:
            content = await env.read_file(path)
        except FileNotFoundError:
            return f"Error: file not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except UnicodeDecodeError as e:
            return f"Error: file is not valid UTF-8: invalid UTF-8 at byte {e.start}."

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

    def __init__(self, allow_create: bool = True):
        # ``allow_create=True`` is the reference behavior (both modes available).
        # When False, the ``create`` / whole-file-overwrite vector is disabled and
        # only ``str_replace`` edits of existing files are permitted — used by the
        # enforcement-on coder toolset so the model cannot create new files or
        # overwrite an existing target wholesale, only edit it in place.
        self.allow_create = allow_create

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        path = params["path"]
        mode = params["mode"]
        env = runtime.environment

        if not env:
            return "Error: no execution environment available."

        if not self.allow_create and mode == "create":
            return (
                "Error: file creation disabled in this phase; edit the existing "
                "target via str_replace (or apply_patch for a content-anchored diff)."
            )

        path = checked_path(runtime, path)

        try:
            async with host_write_lock(path, env):
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
        except UnicodeDecodeError as e:
            return f"Error: refusing to edit non-UTF-8 file: invalid UTF-8 at byte {e.start}."

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

        # A no-op replacement (old_str == new_str) would write the file back
        # unchanged and report a misleading success — the classic "the edit
        # silently did nothing" trap. Surface it as an explicit error so the
        # model knows nothing changed and must supply a real edit.
        if new_str == old_str:
            return (
                f"Error: str_replace was a no-op — old_str and new_str are "
                f"identical; nothing changed in {path}."
            )

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
        if updated == current:
            # Defensive: old_str matched but the resulting content is byte-for-byte
            # identical (e.g. a degenerate overlap). Report no change rather than a
            # misleading success.
            return (
                f"Error: str_replace produced no change in {path} — the file "
                "content is identical after the replacement."
            )
        await env.write_file(path, updated)
        return f"Replaced in {path}: {len(old_str)} chars → {len(new_str)} chars (content changed)"


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
        raw_max_results = params.get("max_results", 50)
        env = runtime.environment

        if not env:
            return "Error: no execution environment available."
        try:
            search_path = checked_path(runtime, search_path)
        except PermissionError as exc:
            return f"Error: {exc}"
        try:
            max_results = int(raw_max_results)
        except (TypeError, ValueError):
            return "Error: max_results must be an integer."
        max_results = max(1, min(max_results, 500))

        quoted_pattern = shlex.quote(pattern)
        quoted_search_path = shlex.quote(search_path)
        rg_cmd = f"rg -n --max-count {max_results} "
        if glob_pattern:
            rg_cmd += f"-g {shlex.quote(glob_pattern)} "
        rg_cmd += f"-- {quoted_pattern} {quoted_search_path} 2>/dev/null"
        # Fallback when rg is missing from PATH: grep MUST use -E (ERE) so that
        # `a|b|c` alternation works. Plain grep is BRE, where `|` is a literal
        # character — an alternation pattern then silently matches nothing. Also
        # exclude VCS/venv and the .opencollab session dir so the fallback can't
        # "match" its own logged pattern strings instead of real source.
        rg_cmd += (
            " || grep -rEn"
            " --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.opencollab "
        )
        if glob_pattern:
            rg_cmd += f"--include={shlex.quote(glob_pattern)} "
        rg_cmd += f"-- {quoted_pattern} {quoted_search_path} 2>/dev/null | head -n {max_results}"

        result = await env.exec_cmd(rg_cmd, timeout=30)
        if result.stdout.strip():
            return truncate(result.stdout.strip(), self.max_grep_chars)
        return f"No matches found for pattern: {pattern}"
