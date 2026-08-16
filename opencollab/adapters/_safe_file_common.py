"""Shared internals for path-based and descriptor-relative safe-file helpers."""

from __future__ import annotations

from typing import TextIO

_READ_CHUNK_BYTES = 1024 * 1024
_RANGE_READ_CHUNK_CHARS = 64 * 1024
_RANGE_TOTAL_COUNT_LIMIT_BYTES = 256 * 1024


def _require_limit(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    return max_bytes


def _read_text_line(
    handle: TextIO,
    *,
    collect_limit: int | None,
) -> tuple[str | None, bool, bool]:
    parts: list[str] = []
    seen_content = False
    remaining = collect_limit
    while True:
        chunk = handle.readline(_RANGE_READ_CHUNK_CHARS)
        if chunk == "":
            if not seen_content:
                return None, True, False
            return "".join(parts), True, False
        seen_content = True
        ended = chunk.endswith("\n")
        content = chunk[:-1] if ended else chunk
        if remaining is not None:
            if len(content) > remaining:
                parts.append(content[:remaining])
                return "".join(parts), False, True
            parts.append(content)
            remaining -= len(content)
        if ended:
            return "".join(parts), False, False
