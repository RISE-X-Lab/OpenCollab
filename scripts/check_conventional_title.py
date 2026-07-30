#!/usr/bin/env python3
"""Validate the Conventional Commit title used by a PR or direct main push."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

_TITLE = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|revert)"
    r"(\([a-z0-9._/ -]+\))?!?: .+$"
)
_ENGLISH_SUMMARY = re.compile(r"(?=.*[A-Za-z])[ -~]+")


def validate_title(title: str) -> str | None:
    """Return an error message when *title* violates the repository convention."""
    if "\n" in title or "\r" in title:
        return "title must be a single line"
    if not _TITLE.fullmatch(title):
        return "title must follow Conventional Commits"
    summary = title.split(": ", 1)[1]
    if not _ENGLISH_SUMMARY.fullmatch(summary):
        return "title summary must use English text"
    return None


def commit_subject(repository: Path, commit: str) -> str:
    """Read the canonical subject from the commit object named by *commit*."""
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%s", commit],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.rstrip("\n")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--title")
    source.add_argument("--commit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        title = args.title if args.title is not None else commit_subject(Path.cwd(), args.commit)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::Unable to read commit title: {exc}")
        return 2
    error = validate_title(title)
    if error:
        print(f"::error::{error}. Received {title!r}.")
        return 1
    print(f"Conventional title check passed for {title!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
