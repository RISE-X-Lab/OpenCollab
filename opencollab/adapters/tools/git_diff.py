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

from opencollab.adapters.tools._output import require_positive_int, truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

# Diffs can be enormous; keep a bounded head+tail (ref: bash.py truncation).
MAX_DIFF_CHARS = 8_000
MAX_STATUS_CHARS = 2_000
MAX_UNTRACKED_DIFF_FILES = 50


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
                "description": "Per-file summary (--stat) instead of the full diff — "
                "see what changed without the line-by-line flood.",
            },
            "include_status": {
                "type": "boolean",
                "description": "Include `git status --short` (default true).",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        max_diff_chars: int = MAX_DIFF_CHARS,
        max_status_chars: int = MAX_STATUS_CHARS,
    ):
        self.max_diff_chars = require_positive_int(
            max_diff_chars, "max_diff_chars"
        )
        self.max_status_chars = require_positive_int(
            max_status_chars, "max_status_chars"
        )

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
        baseline_label = "HEAD"
        if staged:
            diff_cmd += " --cached"
        else:
            head = await env.exec_cmd("git rev-parse --verify HEAD", timeout=30)
            if head.returncode == 0:
                baseline = "HEAD"
            else:
                inside = await env.exec_cmd(
                    "git rev-parse --is-inside-work-tree",
                    timeout=30,
                )
                if inside.returncode != 0 or inside.stdout.strip() != "true":
                    return "Error: not a git repository."
                empty_tree = await env.exec_cmd(
                    "git hash-object -t tree /dev/null",
                    timeout=30,
                )
                baseline = empty_tree.stdout.strip()
                if empty_tree.returncode != 0 or not baseline:
                    error = (empty_tree.stderr or empty_tree.stdout).strip()
                    return (
                        "Error: could not determine Git empty-tree baseline: "
                        + truncate(error, self.max_status_chars)
                    )
                baseline_label = "empty tree"
            diff_cmd += f" {baseline}"
        if stat_only:
            diff_cmd += " --stat"
        diff_cmd += pathspec

        diff_result = await env.exec_cmd(diff_cmd, timeout=30)
        if diff_result.returncode != 0:
            err = (diff_result.stderr or diff_result.stdout).strip()
            if "not a git repository" in err.lower():
                return "Error: not a git repository."
            return f"Error running '{diff_cmd}':\n{truncate(err, self.max_status_chars)}"

        untracked_parts: list[str] = []
        if not staged:
            untracked_cmd = (
                "git --no-pager status --porcelain=v1 -z --untracked-files=all"
                + pathspec
            )
            untracked_result = await env.exec_cmd(untracked_cmd, timeout=30)
            if untracked_result.returncode != 0:
                error = (untracked_result.stderr or untracked_result.stdout).strip()
                return "Error enumerating untracked files:\n" + truncate(
                    error,
                    self.max_status_chars,
                )
            untracked_paths = [
                entry[3:]
                for entry in untracked_result.stdout.split("\0")
                if entry.startswith("?? ")
            ]
            for untracked_path in untracked_paths[:MAX_UNTRACKED_DIFF_FILES]:
                untracked_diff_cmd = "git --no-pager diff --no-index"
                if stat_only:
                    untracked_diff_cmd += " --stat"
                untracked_diff_cmd += (
                    f" -- /dev/null {shlex.quote(untracked_path)}"
                )
                untracked_diff = await env.exec_cmd(untracked_diff_cmd, timeout=30)
                if untracked_diff.returncode not in {0, 1}:
                    error = (untracked_diff.stderr or untracked_diff.stdout).strip()
                    untracked_parts.append(
                        f"{untracked_path}: untracked diff unavailable"
                        + (f" ({truncate(error, 200)})" if error else "")
                    )
                    continue
                text = untracked_diff.stdout.strip()
                if text:
                    untracked_parts.append(text)
                if getattr(untracked_diff, "stdout_truncated", False):
                    untracked_parts.append(
                        f"{untracked_path}: untracked diff output was truncated"
                    )
            if len(untracked_paths) > MAX_UNTRACKED_DIFF_FILES:
                untracked_parts.append(
                    f"... {len(untracked_paths) - MAX_UNTRACKED_DIFF_FILES} "
                    "additional untracked files omitted"
                )

        parts: list[str] = []
        if include_status:
            status_result = await env.exec_cmd("git --no-pager status --short", timeout=30)
            status = status_result.stdout.strip()
            parts.append(
                "Status (git status --short):\n" + truncate(status, self.max_status_chars)
                if status
                else "Status: working tree clean."
            )

        diff_sections = [diff_result.stdout.strip()]
        if untracked_parts:
            diff_sections.append(
                "Untracked files:\n" + "\n".join(untracked_parts)
            )
        diff = "\n".join(section for section in diff_sections if section)
        if stat_only:
            label = (
                "diff --stat"
                if staged or baseline_label == "HEAD"
                else "diff vs empty tree --stat"
            )
        else:
            label = "staged diff" if staged else f"diff vs {baseline_label}"
        if diff:
            parts.append(f"{label}:\n" + truncate(diff, self.max_diff_chars))
        else:
            parts.append(f"{label}: (no changes)")

        return "\n\n".join(parts)
