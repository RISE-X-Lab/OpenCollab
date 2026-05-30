"""Robust edit tool — unified-diff and line-range edits.

``file_write``'s ``str_replace`` mode fails whenever the model's quoted text
doesn't byte-match the file (stray whitespace, a duplicated line, a slightly
stale copy). This tool gives the agent two more forgiving — but still
all-or-nothing — ways to edit:

- ``unified_diff``: apply a standard ``@@`` unified diff. Hunks are located by
  *content* (context + removed lines), tolerating line-number drift, so a diff
  generated against a slightly different revision still lands.
- ``line_replace``: replace an explicit 1-based inclusive line range with new
  text, with an optional ``expected_str`` guard that aborts on a stale range.

Both modes build the full new file in memory and only write if the *entire*
edit applies; a failure returns a clear error and touches nothing — never a
silent partial apply.

Ref:
- claude-code Edit / apply_patch: content-anchored hunks beat exact str match.
- opencode edit tool: str_replace is the baseline; this is the fallback.
"""

from __future__ import annotations

import os
import re
from typing import Any

from filelock import FileLock

from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

# A hunk header: @@ -<old_start>[,<old_len>] +<new_start>[,<new_len>] @@ [heading]
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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
        "- 'unified_diff': apply a standard unified diff (`@@ -a,b +c,d @@` hunks). "
        "Hunks are matched by content, so small line-number drift is tolerated. "
        "Use this when an edit spans multiple places or str_replace keeps failing.\n"
        "- 'line_replace': replace the inclusive 1-based line range "
        "[start_line, end_line] with new_str. Set end_line = start_line - 1 to "
        "insert before start_line without deleting anything. Pass expected_str to "
        "verify the current range before replacing.\n"
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
        safety_policy = runtime.safety_policy

        if safety_policy:
            path = safety_policy.check_path(path)

        lock = FileLock(f"{path}.lock", timeout=10)
        try:
            with lock:
                try:
                    if env:
                        current = await env.read_file(path)
                    else:
                        with open(path, "r", encoding="utf-8") as f:
                            current = f.read()
                except FileNotFoundError:
                    return f"Error: file not found: {path}"

                if mode == "unified_diff":
                    patch = params.get("patch", "")
                    if not patch.strip():
                        return "Error: patch is required for unified_diff mode."
                    updated, err = _apply_unified_diff(current, patch)
                elif mode == "line_replace":
                    updated, err = _apply_line_replace(current, params)
                else:
                    return f"Error: unknown mode '{mode}'. Use 'unified_diff' or 'line_replace'."

                if err:
                    return f"Error applying patch to {path}: {err}"

                assert updated is not None
                if env:
                    await env.write_file(path, updated)
                else:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(updated)
                return _summary(path, mode, current, updated)

        except PermissionError as e:
            return f"Error: {e}"


# ---------------------------------------------------------------------------
# Line helpers — split/join while preserving a trailing-newline flag.
# ---------------------------------------------------------------------------


def _split_lines(text: str) -> tuple[list[str], bool]:
    """Split into lines without trailing newlines; return (lines, ended_with_nl)."""
    if text == "":
        return [], False
    ended_nl = text.endswith("\n")
    lines = text.split("\n")
    if ended_nl:
        lines = lines[:-1]
    return lines, ended_nl


def _join_lines(lines: list[str], ended_nl: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if ended_nl else "")


def _summary(path: str, mode: str, before: str, after: str) -> str:
    before_n = len(before.splitlines())
    after_n = len(after.splitlines())
    return (
        f"Applied {mode} to {path}: {before_n} -> {after_n} lines "
        f"({len(before)} -> {len(after)} chars)"
    )


# ---------------------------------------------------------------------------
# line_replace mode
# ---------------------------------------------------------------------------


def _apply_line_replace(source: str, params: dict[str, Any]) -> tuple[str | None, str]:
    if "start_line" not in params or "end_line" not in params:
        return None, "start_line and end_line are required for line_replace mode."
    start_line = params["start_line"]
    end_line = params["end_line"]
    new_str = params.get("new_str", "")

    lines, ended_nl = _split_lines(source)
    total = len(lines)

    # end_line == start_line - 1 is the insert-before-start_line sentinel.
    if start_line < 1:
        return None, f"start_line must be >= 1 (got {start_line})."
    if end_line < start_line - 1:
        return None, (
            f"end_line ({end_line}) must be >= start_line - 1 ({start_line - 1})."
        )
    if start_line > total + 1:
        return None, (
            f"start_line {start_line} is past end of file ({total} lines)."
        )
    if end_line > total:
        return None, f"end_line {end_line} is past end of file ({total} lines)."

    start_idx = start_line - 1
    end_idx = end_line  # exclusive

    expected = params.get("expected_str")
    if expected is not None:
        actual = "\n".join(lines[start_idx:end_idx])
        if actual != expected.rstrip("\n"):
            return None, (
                "expected_str does not match the current content of lines "
                f"{start_line}-{end_line}.\n--- expected ---\n{expected}\n"
                f"--- actual ---\n{actual}"
            )

    # Preserve the file's trailing-newline convention; new_str's own trailing
    # newline is normalised away since we re-split it into lines.
    new_lines, _ = _split_lines(new_str if new_str.endswith("\n") or new_str == "" else new_str + "\n")
    result = lines[:start_idx] + new_lines + lines[end_idx:]
    return _join_lines(result, ended_nl if result else False), ""


# ---------------------------------------------------------------------------
# unified_diff mode
# ---------------------------------------------------------------------------


def _parse_hunks(patch: str) -> tuple[list[dict] | None, str]:
    hunks: list[dict] = []
    cur: dict | None = None
    seen_header = False
    raw_lines = patch.split("\n")
    # Drop the empty element produced by a trailing newline; a genuine blank
    # context line in a unified diff is " " (a lone space), not "".
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for raw in raw_lines:
        m = _HUNK_RE.match(raw)
        if m:
            seen_header = True
            cur = {"old_start": int(m.group(1)), "lines": []}
            hunks.append(cur)
            continue
        if cur is None:
            # Lines before the first @@ (e.g. ---/+++ headers) are ignored.
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw == "":
            # A blank line inside a hunk is a blank context line.
            cur["lines"].append(" ")
            continue
        tag = raw[0]
        if tag in " +-":
            cur["lines"].append(raw)
        elif tag == "\\":
            # "\ No newline at end of file" — metadata, not content.
            continue
        else:
            # Unexpected content ends the current hunk (trailing prose, etc.).
            cur = None
    if not seen_header:
        return None, "no @@ hunk headers found in patch."
    if not hunks:
        return None, "patch contains no applicable hunks."
    return hunks, ""


def _find_block(
    src_lines: list[str], old_block: list[str], expected_idx: int, min_idx: int
) -> int | None:
    """Locate ``old_block`` in ``src_lines`` at or after ``min_idx``.

    Picks the occurrence nearest the diff's stated position so repeated blocks
    resolve to the intended one. Returns None if the block isn't found.
    """
    if not old_block:
        # Pure insertion: clamp the stated position into the valid range.
        return max(min_idx, min(expected_idx, len(src_lines)))
    n = len(old_block)
    matches = [
        start
        for start in range(min_idx, len(src_lines) - n + 1)
        if src_lines[start : start + n] == old_block
    ]
    if not matches:
        return None
    matches.sort(key=lambda s: abs(s - expected_idx))
    return matches[0]


def _apply_unified_diff(source: str, patch: str) -> tuple[str | None, str]:
    hunks, err = _parse_hunks(patch)
    if err:
        return None, err
    assert hunks is not None

    src_lines, ended_nl = _split_lines(source)
    result: list[str] = []
    src_idx = 0  # how far through src_lines we've consumed

    for n, hunk in enumerate(hunks, 1):
        old_block: list[str] = []
        new_block: list[str] = []
        for line in hunk["lines"]:
            tag, content = line[0], line[1:]
            if tag == " ":
                old_block.append(content)
                new_block.append(content)
            elif tag == "-":
                old_block.append(content)
            elif tag == "+":
                new_block.append(content)

        pos = _find_block(src_lines, old_block, hunk["old_start"] - 1, src_idx)
        if pos is None:
            snippet = "\n".join(old_block[:6]) or "(empty context)"
            return None, (
                f"hunk #{n} (near line {hunk['old_start']}) did not match the file. "
                f"The context/removed lines were not found:\n{snippet}"
            )
        result.extend(src_lines[src_idx:pos])
        result.extend(new_block)
        src_idx = pos + len(old_block)

    result.extend(src_lines[src_idx:])
    return _join_lines(result, ended_nl if result else False), ""
