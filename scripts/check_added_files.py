#!/usr/bin/env python3
"""Enforce size and line-count limits for files added by a Git change."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from pathlib import Path

MAX_BYTES = 512_000
MAX_PY_LINES = 800


def _added_paths(repository: Path, base: str, head: str) -> list[str]:
    with tempfile.TemporaryFile() as paths_file:
        subprocess.run(
            [
                "git",
                "diff",
                "--diff-filter=A",
                "--name-only",
                "-z",
                base,
                head,
            ],
            cwd=repository,
            check=True,
            stdout=paths_file,
            stderr=subprocess.PIPE,
        )
        paths_file.seek(0)
        return [
            os.fsdecode(raw_path)
            for raw_path in paths_file.read().split(b"\0")
            if raw_path
        ]


def _command_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _command_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _error(path: str, message: str) -> None:
    print(
        f"::error file={_command_property(path)}::{_command_message(message)}",
        flush=True,
    )


def check_added_files(
    repository: Path,
    base: str,
    head: str,
    *,
    max_bytes: int = MAX_BYTES,
    max_py_lines: int = MAX_PY_LINES,
    require_files: bool = False,
) -> list[str]:
    """Return human-readable violations for regular files added by the change."""
    violations: list[str] = []
    added_paths = _added_paths(repository, base, head)
    if require_files and not added_paths:
        return ["repository: no files were available for the complete-tree check"]
    for relative in added_paths:
        path = repository / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if metadata.st_size > max_bytes:
            violations.append(
                f"{relative}: new file is {metadata.st_size} bytes, limit is {max_bytes}"
            )
        if path.suffix == ".py":
            with path.open("rb") as handle:
                lines = sum(1 for _line in handle)
            if lines > max_py_lines:
                violations.append(
                    f"{relative}: new Python module is {lines} lines, limit is {max_py_lines}"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--require-files", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    repository = Path.cwd()
    try:
        violations = check_added_files(
            repository,
            args.base,
            args.head,
            require_files=args.require_files,
        )
    except subprocess.CalledProcessError as exc:
        print(f"git diff failed with exit code {exc.returncode}", file=sys.stderr)
        return 2
    if not violations:
        print("Added-file hygiene checks passed.")
        return 0
    for violation in violations:
        path, _separator, message = violation.partition(": ")
        _error(path, message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
