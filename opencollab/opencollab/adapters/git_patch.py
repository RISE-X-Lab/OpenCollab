"""Build fail-closed Git patch extraction commands."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def guarded_staged_diff_command(
    *,
    base_revision: str = "HEAD",
    exclude_paths: Sequence[str] = (),
) -> str:
    """Stage through a temporary index and reject unknown reserved paths."""
    if not isinstance(base_revision, str) or not base_revision.strip() or "\0" in base_revision:
        raise ValueError("patch base revision is invalid")

    resets = ""
    for path in exclude_paths:
        if str(path).strip():
            resets += (
                'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q '
                f"{shlex.quote(base_revision)} -- {shlex.quote(str(path))} && "
            )
    reserved_guard = (
        'if GIT_INDEX_FILE="$idx" git diff --cached --quiet '
        f"{shlex.quote(base_revision)} -- "
        "':(glob,top).opencollab-retired-*' "
        "':(glob,top)**/.opencollab-retired-*' "
        "':(glob,top).opencollab-retired-*/**' "
        "':(glob,top)**/.opencollab-retired-*/**'; then :; "
        "else reserved_rc=$?; "
        'if [ "$reserved_rc" -eq 1 ]; then '
        "echo 'unregistered or modified .opencollab-retired-* path in candidate patch' >&2; exit 125; "
        'else exit "$reserved_rc"; fi; fi; '
    )
    return (
        'idx=$(mktemp) || exit 125; trap \'rm -f -- "$idx"\' EXIT; '
        f"GIT_INDEX_FILE=\"$idx\" git read-tree {shlex.quote(base_revision)} && "
        'GIT_INDEX_FILE="$idx" git add -f -A && '
        f"{resets}"
        f"{reserved_guard}"
        'GIT_INDEX_FILE="$idx" git diff --cached --binary '
        f"{shlex.quote(base_revision)}"
    )


__all__ = ["guarded_staged_diff_command"]
