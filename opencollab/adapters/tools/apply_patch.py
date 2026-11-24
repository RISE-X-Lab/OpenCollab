"""Apply unified diffs or line-range edits to a single file.

``unified_diff`` locates hunks by context and removed content, tolerating
line-number drift. ``line_replace`` replaces a 1-based inclusive range with
an optional ``expected_str`` check for stale content.

Both modes build the full result before writing. Any failed hunk or range
leaves the file unchanged.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters._env_base import WriteRejectedWithoutChanges
from opencollab.adapters.tools._paths import checked_path
from opencollab.adapters.tools.apply_patch_engine import (
    _apply_line_replace,
    _apply_unified_diff,
    _summary,
)
from opencollab.adapters.tools.base import Tool, host_write_lock
from opencollab.application.tool_execution import ToolRuntime


class ApplyPatchTool(Tool):
    """Apply a unified diff or line-range replacement to a single file.

    Write only after every hunk or range applies successfully.
    """

    name = "apply_patch"
    description = (
        "Apply a robust, all-or-nothing edit to ONE file. Modes:\n"
        "- 'unified_diff': apply a standard unified diff (`@@ -a,b +c,d @@` hunks). "
        "Hunks are matched by content, so small line-number drift is tolerated. "
        "Use this when an edit spans multiple places or file_write's str_replace keeps failing.\n"
        "- 'line_replace': replace the inclusive 1-based line range "
        "[start_line, end_line] with new_str. Set end_line = start_line - 1 to "
        "insert before start_line without deleting anything. Pass expected_str to "
        "verify the current range before replacing. Set relocate_expected=true to "
        "relocate stale coordinates using exactly one matching quoted range.\n"
        "Set normalize_hunks=true to recompute hunk counts and accept bare @@ "
        "headers when their context identifies exactly one remaining range.\n"
        "str_replace is a mode of the `file_write` tool. This tool accepts "
        "'unified_diff' and 'line_replace'.\n"
        "If the patch/range does not apply cleanly, NOTHING is written and an error "
        "is returned — it never partially applies."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace.",
            },
            "mode": {
                "type": "string",
                "enum": ["unified_diff", "line_replace"],
                "description": "Edit mode.",
            },
            "patch": {
                "type": "string",
                "description": "Unified diff text (for 'unified_diff' mode). "
                "The ---/+++ header lines are optional and ignored; only @@ hunks matter.",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to replace, 1-based inclusive (for 'line_replace').",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to replace, 1-based inclusive (for 'line_replace'). "
                "Use start_line - 1 to insert without deleting.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text (for 'line_replace' mode).",
            },
            "normalize_hunks": {
                "type": "boolean",
                "description": "Recompute hunk counts and accept uniquely anchored bare @@ headers. Default false.",
            },
            "relocate_expected": {
                "type": "boolean",
                "description": "Relocate a stale line range by one exact expected_str match. Default false.",
            },
            "expected_str": {
                "type": "string",
                "description": "Optional guard (for 'line_replace'): the exact current text of "
                "[start_line, end_line]. If it doesn't match, the edit aborts.",
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
        observations = getattr(runtime, "observations", None)
        if observations is not None:
            observations.record_write(completed=False, changed=False)

        if env is None:
            return "Error: no execution environment available."

        path = checked_path(runtime, path, native_file=True)

        try:
            async with host_write_lock(path, env):
                try:
                    current = await env.read_file(path)
                except FileNotFoundError:
                    return f"Error: file not found: {path}"

                notes: list[str] = []
                if mode == "unified_diff":
                    patch = params.get("patch", "")
                    if not patch.strip():
                        return "Error: patch is required for unified_diff mode."
                    updated, err = _apply_unified_diff(
                        current, patch, normalize_hunks=params.get("normalize_hunks", False)
                    )
                elif mode == "line_replace":
                    updated, err = _apply_line_replace(current, params, notes=notes)
                else:
                    return (
                        f"Error: unknown mode '{mode}'. Use 'unified_diff' or 'line_replace'. "
                        "str_replace is a mode of the file_write tool."
                    )

                if err:
                    return f"Error applying patch to {path}: {err}"

                assert updated is not None
                if updated == current:
                    return (
                        f"Error applying patch to {path}: patch produced no changes."
                    )
                if observations is not None:
                    observations.record_write(completed=False, changed=None, path=path)
                await env.write_file(path, updated)
                if observations is not None:
                    observations.record_write(completed=True, changed=True, path=path)
                summary = _summary(path, mode, current, updated)
                if notes:
                    summary += "\nNote: " + ". ".join(notes) + "."
                return summary

        except WriteRejectedWithoutChanges as e:
            if observations is not None:
                observations.record_write(completed=False, changed=False, path=path)
            raise OSError(*e.args) from e
        except PermissionError as e:
            return f"Error: {e}"
        except UnicodeDecodeError as e:
            return f"Error: refusing to edit non-UTF-8 file: invalid UTF-8 at byte {e.start}."
