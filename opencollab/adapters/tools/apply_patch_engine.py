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
# A hunk header carrying no position at all. Models emit these constantly --
# it is the dialect several diff-editing tools teach -- and they cost this
# applier nothing, because hunks are located by content and the numbers were
# only ever a hint about where to start looking.
_BARE_HUNK_RE = re.compile(r"^@@[ \t]*(?:@@[ \t]*)?$")
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


def _locate_expected(lines: list[str], wanted: str) -> tuple[tuple[int, int] | None, int]:
    """Find ``wanted`` in ``lines``, but only when it occurs exactly once.

    Returns ``((start_idx, end_idx), count)`` for a unique match and
    ``(None, count)`` otherwise, so the caller can say which of the two
    failures it hit.

    A line range is a coordinate and the quoted text is evidence; when the two
    disagree, the text is the one that can be checked against the file. A
    single exact occurrence identifies the range at least as strongly as the
    numbers did. Two occurrences identify nothing, and that stays an error
    rather than becoming a guess.
    """
    block = wanted.split("\n")
    if not block or len(block) > len(lines):
        return None, 0
    n = len(block)
    hits = [
        start
        for start in range(0, len(lines) - n + 1)
        if lines[start : start + n] == block
    ]
    if len(hits) != 1:
        return None, len(hits)
    return (hits[0], hits[0] + n), 1


def _apply_line_replace(
    source: str,
    params: dict[str, Any],
    notes: list[str] | None = None,
) -> tuple[str | None, str]:
    """Replace the 1-based inclusive line range from ``params`` in ``source``.

    Returns ``(updated_text, "")`` on success or ``(None, error)`` on failure.
    ``notes`` collects anything the caller must be told about how the edit was
    placed -- specifically, that it landed somewhere other than the range asked
    for.
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
        wanted = expected.rstrip("\n")
        actual = "\n".join(lines[start_idx:end_idx])
        if actual != wanted:
            located, occurrences = _locate_expected(lines, wanted)
            if located is None:
                whereabouts = (
                    "and it does not appear anywhere in the file"
                    if occurrences == 0
                    else f"and it appears {occurrences} times, so no single range "
                    "is identified by it"
                )
                return None, (
                    "expected_str does not match the current content of lines "
                    f"{start_line}-{end_line}, {whereabouts}.\n"
                    f"--- expected ---\n{expected}\n--- actual ---\n{actual}"
                )
            start_idx, end_idx = located
            if notes is not None:
                notes.append(
                    f"expected_str did not match lines {start_line}-{end_line}; it "
                    f"matched lines {start_idx + 1}-{end_idx} uniquely, and the edit "
                    "was applied there"
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
        bare = _BARE_HUNK_RE.match(raw) if m is None else None
        if m is not None or bare is not None:
            if cur is not None:
                err = _finish_hunk(cur)
                if err:
                    return None, err
            seen_header = True
            cur = {
                # ``None`` means the header named no position. The applier then
                # takes the first match after the previous hunk, which is what a
                # sequential diff means anyway.
                "old_start": int(m.group(1)) if m is not None else None,
                "new_start": int(m.group(3)) if m is not None else None,
                "header_line": line_number,
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
    err = _finish_hunk(cur)
    if err:
        return None, err
    return hunks, ""


def _hunk_where(hunk: dict) -> str:
    """Name a hunk in an error, whether or not its header carried a position."""
    start = hunk.get("old_start")
    if start is None:
        return f"whose header is on patch line {hunk['header_line']}"
    return f"near line {start}"


def _finish_hunk(hunk: dict) -> str:
    """Fill in a hunk's line counts from its body, and check what is checkable.

    The ``@@ -a,b +c,d @@`` counts are redundant to this applier: every hunk is
    located by matching its context and removed lines against the file, and
    ``b`` and ``d`` are never read again. A model that miscounted them has still
    described the edit exactly, so the body is taken as the truth and the
    declared numbers are dropped rather than turned into a rejection -- a patch
    that does not describe the file is still refused, by the content match,
    which is the check that carries information.

    What the body cannot settle is still an error: a hunk with no body at all,
    and a header that starts at line 0 while keeping or removing lines, since
    there is no line 0 to start from.
    """
    hunk["old_len"] = sum(line[0] in " -" for line in hunk["lines"])
    hunk["new_len"] = sum(line[0] in " +" for line in hunk["lines"])
    where = _hunk_where(hunk)
    if not hunk["lines"]:
        return f"hunk {where} has no body."
    if hunk["old_start"] == 0 and hunk["old_len"] != 0:
        return f"hunk {where} starts at old line 0 but keeps or removes lines."
    if hunk["new_start"] == 0 and hunk["new_len"] != 0:
        return f"hunk {where} starts at new line 0 but keeps or adds lines."
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

        if hunk["old_start"] is None:
            expected_idx = src_idx
        elif not old_block:
            expected_idx = hunk["old_start"]
        else:
            expected_idx = hunk["old_start"] - 1
        pos = _find_block(src_lines, old_block, expected_idx, src_idx)
        if pos is None:
            snippet = "\n".join(old_block[:6]) or "(empty context)"
            return None, (
                f"hunk #{n} ({_hunk_where(hunk)}) did not match the file. "
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
