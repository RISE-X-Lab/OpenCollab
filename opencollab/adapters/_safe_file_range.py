"""Read bounded logical-line windows from already opened text files."""

from __future__ import annotations

from typing import TextIO

from opencollab.adapters._env_base import TextFileRange
from opencollab.adapters._safe_file_common import _read_text_line


def _read_text_range(
    handle: TextIO,
    *,
    offset: int,
    limit: int,
    max_chars: int,
    count_to_eof: bool,
) -> TextFileRange:
    line_number = 0
    while line_number < offset - 1:
        line, at_eof, _truncated = _read_text_line(
            handle,
            collect_limit=None,
        )
        if line is None:
            return TextFileRange([], offset, line_number, False)
        line_number += 1
        if at_eof:
            return TextFileRange([], offset, line_number, False)

    selected: list[str] = []
    chars_used = 0
    while len(selected) < limit:
        separator_chars = 1 if selected else 0
        remaining = max_chars - chars_used - separator_chars
        if remaining < 1:
            return TextFileRange(
                selected,
                offset,
                None,
                True,
                chars_truncated=True,
            )
        line, at_eof, truncated = _read_text_line(
            handle,
            collect_limit=remaining,
        )
        if line is None:
            return TextFileRange(
                selected,
                offset,
                line_number,
                False,
            )
        selected.append(line)
        line_number += 1
        chars_used += separator_chars + len(line)
        if truncated:
            return TextFileRange(
                selected,
                offset,
                line_number if at_eof else None,
                True,
                chars_truncated=True,
            )
        if at_eof:
            return TextFileRange(
                selected,
                offset,
                line_number,
                False,
            )

    if count_to_eof:
        shown_end = offset - 1 + len(selected)
        while True:
            line, at_eof, _truncated = _read_text_line(
                handle,
                collect_limit=None,
            )
            if line is None:
                return TextFileRange(
                    selected,
                    offset,
                    line_number,
                    line_number > shown_end,
                )
            line_number += 1
            if at_eof:
                return TextFileRange(
                    selected,
                    offset,
                    line_number,
                    line_number > shown_end,
                )
    has_more = handle.read(1) != ""
    return TextFileRange(
        selected,
        offset,
        None if has_more else line_number,
        has_more,
    )
