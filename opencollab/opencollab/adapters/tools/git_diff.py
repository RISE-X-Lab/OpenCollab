"""git_diff — focused working-tree diff and status.

Reviewers and the lead need to see *what actually changed* in the working tree
to judge a fix, without burning budget reconstructing it from a transcript of
edits. This tool returns ``git status --short`` plus the working-tree diff
against HEAD (all uncommitted changes), focused by an optional pathspec and
truncated so a large diff can't blow up the context.

Read-only: it never stages, commits, or resets — only ``git status`` and
``git diff``.

Ref:
- bash.py: same env handling + head/tail truncation.
- grep.py: read-only tools skip the command-safety gate.
"""

from __future__ import annotations

import shlex
from typing import Any

from opencollab.adapters.tools._output import truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

# Diffs can be enormous; keep a bounded head+tail (ref: bash.py truncation).
MAX_DIFF_CHARS = 8_000
MAX_STATUS_CHARS = 2_000


class GitDiffTool(Tool):
    """Show the current working-tree diff (and status) vs HEAD.

    Use this to review what a teammate changed before judging a fix, or to
    confirm your own edits landed. Pass `path` to focus on one file/directory,
    or `stat_only` for a per-file summary instead of the full diff.
    """

    name = "git_diff"
    description = (
        "Show the current working-tree changes (uncommitted diff vs HEAD) plus a "
        "short status. Read-only — never stages or commits. Pass `path` to focus on "
        "a file or directory, `staged: true` to see only staged changes, or "
        "`stat_only: true` for a per-file summary instead of the full diff. Output "
        "is truncated to protect context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional pathspec to focus the diff (e.g. 'src/foo.py').",
            },
            "staged": {
                "type": "boolean",
                "description": "If true, show only staged changes (git diff --cached).",
            },
            "stat_only": {
                "type": "boolean",
                "description": "If true, show a per-file summary (--stat) instead of the full diff.",
            },
            "include_status": {
                "type": "boolean",
                "description": "Include `git status --short` (default true).",
            },
        },
        "required": [],
    }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        path = params.get("path")
        staged = params.get("staged", False)
        stat_only = params.get("stat_only", False)
        include_status = params.get("include_status", True)
        env = runtime.environment

        if not env:
            return "Error: no execution environment available."

        pathspec = f" -- {shlex.quote(path)}" if path else ""

        diff_cmd = "git --no-pager diff"
        if staged:
            diff_cmd += " --cached"
        if stat_only:
            diff_cmd += " --stat"
        else:
            diff_cmd += " HEAD" if not staged else ""
        diff_cmd += pathspec

        diff_result = await env.exec_cmd(diff_cmd, timeout=30)
        if diff_result.returncode != 0:
            err = (diff_result.stderr or diff_result.stdout).strip()
            if "not a git repository" in err.lower():
                return "Error: not a git repository."
            return f"Error running '{diff_cmd}':\n{truncate(err, MAX_STATUS_CHARS)}"

        parts: list[str] = []
        if include_status:
            status_result = await env.exec_cmd("git --no-pager status --short", timeout=30)
            status = status_result.stdout.strip()
            parts.append(
                "Status (git status --short):\n" + truncate(status, MAX_STATUS_CHARS)
                if status
                else "Status: working tree clean."
            )

        diff = diff_result.stdout.strip()
        label = "diff --stat" if stat_only else ("staged diff" if staged else "diff vs HEAD")
        if diff:
            parts.append(f"{label}:\n" + truncate(diff, MAX_DIFF_CHARS))
        else:
            parts.append(f"{label}: (no changes)")

        return "\n\n".join(parts)
