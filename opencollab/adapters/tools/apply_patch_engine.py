"""The diff engine behind ``apply_patch`` — pure text-to-text functions.

Both modes build the full new file in memory and report failure without
side effects, so the calling tool can stay all-or-nothing. Independently
unit-testable: no I/O, no tool plumbing.
"""

from __future__ import annotations

import re
from typing import Any

# A hunk header: @@ -<old_start>[,<old_len>] +<new_start>[,<new_len>] @@ [heading]
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_MIXED_NEWLINES = "mixed"


# ---------------------------------------------------------------------------
# Line helpers — split/join while preserving a trailing-newline flag.
# ---------------------------------------------------------------------------


def _detect_newline_style(text: str) -> str:
    styles = set(_NEWLINE_RE.findall(text))
    if len(styles) > 1:
        return _MIXED_NEWLINES
    return next(iter(styles), "\n")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_newlines(text: str, style: str) -> str:
    if style == "\n":
        return text
    return text.replace("\n", style)


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
    """Replace the 1-based inclusive line range from ``params`` in ``source``.

    Returns ``(updated_text, "")`` on success or ``(None, error)`` on failure.
    """
    newline_style = _detect_newline_style(source)
    if newline_style == _MIXED_NEWLINES:
        return None, (
            "source uses mixed newline styles; refusing to normalize it implicitly."
        )
    source = _normalize_newlines(source)
    params = dict(params)
    for key in ("new_str", "expected_str"):
        if key in params and params[key] is not None:
            params[key] = _normalize_newlines(params[key])

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
    updated = _join_lines(result, ended_nl if result else False)
    return _restore_newlines(updated, newline_style), ""


# ---------------------------------------------------------------------------
# unified_diff mode
# ---------------------------------------------------------------------------


def _parse_hunks(patch: str) -> tuple[list[dict] | None, str]:
    """Parse and structurally validate ``@@`` hunks from a unified diff."""
    hunks: list[dict] = []
    cur: dict | None = None
    seen_header = False
    raw_lines = patch.split("\n")
    # Drop the empty element produced by a trailing newline; a genuine blank
    # context line in a unified diff is " " (a lone space), not "".
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for line_number, raw in enumerate(raw_lines, 1):
        m = _HUNK_RE.match(raw)
        if m:
            if cur is not None:
                err = _validate_hunk_counts(cur)
                if err:
                    return None, err
            seen_header = True
            old_start = int(m.group(1))
            old_len = int(m.group(2) or 1)
            new_start = int(m.group(3))
            new_len = int(m.group(4) or 1)
            if old_start == 0 and old_len != 0:
                return None, f"hunk header on patch line {line_number} has invalid old range."
            if new_start == 0 and new_len != 0:
                return None, f"hunk header on patch line {line_number} has invalid new range."
            cur = {
                "old_start": old_start,
                "old_len": old_len,
                "new_start": new_start,
                "new_len": new_len,
                "lines": [],
                "no_newline_after": set(),
            }
            hunks.append(cur)
            continue
        if cur is None:
            # Lines before the first @@ (e.g. ---/+++ headers) are ignored.
            continue
        if raw == "":
            return None, f"hunk on patch line {line_number} has an untagged blank line."
        tag = raw[0]
        if tag in " +-":
            cur["lines"].append(raw)
        elif tag == "\\":
            if raw != "\\ No newline at end of file":
                return None, f"hunk on patch line {line_number} has unknown metadata."
            if not cur["lines"]:
                return None, f"hunk on patch line {line_number} has orphan newline metadata."
            previous = len(cur["lines"]) - 1
            if previous in cur["no_newline_after"]:
                return None, f"hunk on patch line {line_number} repeats newline metadata."
            cur["no_newline_after"].add(previous)
        else:
            return None, f"hunk on patch line {line_number} has an unknown line prefix."
    if not seen_header:
        return None, "no @@ hunk headers found in patch."
    if not hunks:
        return None, "patch contains no applicable hunks."
    assert cur is not None
    err = _validate_hunk_counts(cur)
    if err:
        return None, err
    return hunks, ""


def _validate_hunk_counts(hunk: dict) -> str:
    """Return an error unless hunk body counts match its header exactly."""
    old_count = sum(line[0] in " -" for line in hunk["lines"])
    new_count = sum(line[0] in " +" for line in hunk["lines"])
    if old_count != hunk["old_len"] or new_count != hunk["new_len"]:
        return (
            f"hunk near line {hunk['old_start']} declares {hunk['old_len']} old lines and "
            f"{hunk['new_len']} new lines, but contains {old_count} old lines and "
            f"{new_count} new lines."
        )
    return ""


def _find_block(
    src_lines: list[str], old_block: list[str], expected_idx: int, min_idx: int
) -> int | None:
    """Locate ``old_block`` in ``src_lines`` at or after ``min_idx``.

    Picks the occurrence nearest the diff's stated position so repeated blocks
    resolve to the intended one. Returns None if the block isn't found.
    """
    if not old_block:
        if expected_idx < min_idx or expected_idx > len(src_lines):
            return None
        return expected_idx
    n = len(old_block)
    best_start: int | None = None
    best_distance: int | None = None
    for start in range(min_idx, len(src_lines) - n + 1):
        if src_lines[start] != old_block[0]:
            continue
        if any(
            src_lines[start + offset] != old_block[offset]
            for offset in range(1, n)
        ):
            continue
        distance = abs(start - expected_idx)
        if best_distance is None or distance < best_distance:
            best_start = start
            best_distance = distance
            if distance == 0:
                break
    return best_start


def _apply_unified_diff(source: str, patch: str) -> tuple[str | None, str]:
    """Apply a unified diff to ``source``; hunks are located by content.

    Returns ``(updated_text, "")`` on success or ``(None, error)`` on failure.
    """
    newline_style = _detect_newline_style(source)
    if newline_style == _MIXED_NEWLINES:
        return None, (
            "source uses mixed newline styles; refusing to normalize it implicitly."
        )
    source = _normalize_newlines(source)
    patch = _normalize_newlines(patch)
    hunks, err = _parse_hunks(patch)
    if err:
        return None, err
    assert hunks is not None

    src_lines, ended_nl = _split_lines(source)
    result: list[str] = []
    src_idx = 0  # how far through src_lines we've consumed
    target_ended_nl = ended_nl

    for n, hunk in enumerate(hunks, 1):
        old_block: list[str] = []
        new_block: list[str] = []
        old_no_newline_positions: list[int] = []
        new_no_newline_positions: list[int] = []
        for line_index, line in enumerate(hunk["lines"]):
            tag, content = line[0], line[1:]
            if tag == " ":
                old_block.append(content)
                new_block.append(content)
            elif tag == "-":
                old_block.append(content)
            elif tag == "+":
                new_block.append(content)
            if line_index in hunk["no_newline_after"]:
                if tag in " -":
                    old_no_newline_positions.append(len(old_block) - 1)
                if tag in " +":
                    new_no_newline_positions.append(len(new_block) - 1)

        expected_idx = hunk["old_start"] if hunk["old_len"] == 0 else hunk["old_start"] - 1
        pos = _find_block(src_lines, old_block, expected_idx, src_idx)
        if pos is None:
            snippet = "\n".join(old_block[:6]) or "(empty context)"
            return None, (
                f"hunk #{n} (near line {hunk['old_start']}) did not match the file. "
                f"The context/removed lines were not found:\n{snippet}"
            )
        if hunk["no_newline_after"]:
            if n != len(hunks) or pos + len(old_block) != len(src_lines):
                return None, (
                    f"hunk #{n} has 'No newline at end of file' metadata away from EOF."
                )
            if (
                len(old_no_newline_positions) > 1
                or old_no_newline_positions
                and old_no_newline_positions[0] != len(old_block) - 1
                or len(new_no_newline_positions) > 1
                or new_no_newline_positions
                and new_no_newline_positions[0] != len(new_block) - 1
            ):
                return None, f"hunk #{n} has newline metadata on a non-final line."
            old_has_no_newline = bool(old_no_newline_positions)
            if ended_nl == old_has_no_newline:
                return None, (
                    f"hunk #{n} newline metadata does not match the source EOF."
                )
            target_ended_nl = not bool(new_no_newline_positions)
        result.extend(src_lines[src_idx:pos])
        result.extend(new_block)
        src_idx = pos + len(old_block)

    result.extend(src_lines[src_idx:])
    updated = _join_lines(result, target_ended_nl if result else False)
    return _restore_newlines(updated, newline_style), ""
