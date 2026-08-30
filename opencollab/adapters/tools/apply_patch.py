"""Robust edit tool — unified-diff and line-range edits.

``file_write``'s ``str_replace`` mode fails whenever the model's quoted text
doesn't byte-match the file (stray whitespace, a duplicated line, a slightly
stale copy). This tool gives the agent two more forgiving — but still
all-or-nothing — ways to edit:

- ``unified_diff``: apply a standard ``@@`` unified diff. Hunks are located by
  *content* (context + removed lines), tolerating line-number drift, so a diff
  generated against a slightly different revision still lands.
- ``line_replace``: replace an explicit 1-based inclusive line range with new
  text, with an optional ``expected_str`` naming the text meant to be replaced.

Because both modes locate the edit by content, every coordinate a caller
supplies is a hint rather than a fact, and a wrong hint is not fatal. A hunk
header may carry no numbers at all (a bare ``@@``), its declared ``-a,b +c,d``
counts are recomputed from the body rather than checked against it, and a
``line_replace`` whose range is stale still lands when its ``expected_str``
occurs exactly once. Measured on 30 pilot runs, those three cases were 34 of
the 35 ``apply_patch`` failures: in every one the model had described the edit
correctly and named the position wrongly. What stays fatal is the check that
carries information -- text that does not match the file, or matches it in more
than one place.

Both modes build the full new file in memory and only write if the *entire*
edit applies; a failure returns a clear error and touches nothing — never a
silent partial apply.

Ref:
- claude-code Edit / apply_patch: content-anchored hunks beat exact str match.
- opencode edit tool: str_replace is the baseline; this is the fallback.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools._paths import checked_path
from opencollab.adapters.tools.apply_patch_engine import (
    _apply_line_replace,
    _apply_unified_diff,
    _summary,
)
from opencollab.adapters.tools.base import Tool, host_write_lock
from opencollab.application.tool_execution import ToolRuntime


class ApplyPatchTool(Tool):
    """Apply a unified diff or a line-range replacement to a single file.

    Prefer this over ``file_write`` str_replace when an edit keeps failing to
    match (whitespace differences, duplicate lines, drifted line numbers). The
    edit is applied atomically: if any hunk or range can't be placed cleanly,
    nothing is written and a clear error is returned.
    """

    name = "apply_patch"
    description = (
        "Apply a robust, all-or-nothing edit to ONE file. Modes:\n"
        "- 'unified_diff': apply a unified diff. Hunks are located by their CONTENT "
        "(context and removed lines), never by line number: the `-a,b +c,d` counts "
        "are recomputed from the hunk body instead of being checked, and a bare `@@` "
        "header carrying no numbers is accepted. Get the lines right; do not spend "
        "effort on the header.\n"
        "- 'line_replace': replace the inclusive 1-based line range "
        "[start_line, end_line] with new_str. Set end_line = start_line - 1 to "
        "insert before start_line without deleting anything. Pass expected_str to "
        "name the text you mean to replace: if your line numbers are stale but that "
        "text occurs exactly once in the file, the edit lands on it and the result "
        "says so.\n"
        "There is no 'str_replace' mode here — str_replace is a mode of the "
        "`file_write` tool.\n"
        "If the patch or range does not apply cleanly, NOTHING is written and an "
        "error is returned — it never partially applies."
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

        if env is None:
            return "Error: no execution environment available."

        path = checked_path(runtime, path)

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
                    updated, err = _apply_unified_diff(current, patch)
                elif mode == "line_replace":
                    updated, err = _apply_line_replace(current, params, notes=notes)
                else:
                    return (
                        f"Error: unknown mode '{mode}'. This tool has "
                        "'unified_diff' and 'line_replace'; str_replace is a mode of "
                        "the file_write tool."
                    )

                if err:
                    return f"Error applying patch to {path}: {err}"

                assert updated is not None
                if updated == current:
                    return (
                        f"Error applying patch to {path}: patch produced no changes."
                    )
                await env.write_file(path, updated)
                summary = _summary(path, mode, current, updated)
                # The caller asked for one range and got another: say so, or a
                # later edit is planned against line numbers that moved.
                if notes:
                    summary += "\nNote: " + "; ".join(notes) + "."
                return summary

        except PermissionError as e:
            return f"Error: {e}"
        except UnicodeDecodeError as e:
            return f"Error: refusing to edit non-UTF-8 file: invalid UTF-8 at byte {e.start}."
