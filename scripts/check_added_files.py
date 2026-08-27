#!/usr/bin/env python3
"""Enforce size and line-count limits on the files a Git change touches.

A change is rejected when it pushes a file *past* a ceiling: either it adds a
file that is already too large, or it grows a compliant file until it is not.
Appending to an existing module is the way these limits actually get broken, so
"was the file created here" is not the question worth asking.

A file that already broke a ceiling before the change stays that way without
failing every later pull request that happens to touch it. Those are caught by
the complete-tree run on ``main`` instead, which measures every file absolutely.

The two ceilings carry different weight. The byte ceiling is a hard gate: a
large binary in history cannot be taken back out. The line ceiling is advisory
— module size predicts little on its own, and splitting a file to satisfy a
number usually widens its public interface instead of narrowing it. So a broken
line ceiling is reported as a warning and does not fail the run; interface width
is the gate that judges a split.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from typing import NamedTuple

MAX_BYTES = 512_000
# Advisory only: reported as a warning, never counted towards the exit code.
MAX_PY_LINES = 800
ADVISORY_LIMITS = frozenset({"lines"})


class Violation(NamedTuple):
    """A broken ceiling: which one, where, and how it was broken."""

    limit: str
    path: str
    message: str


def _changed_paths(repository: Path, base: str, head: str) -> list[str]:
    """Return every regular path the change adds or modifies.

    Renames are reported as an add plus a delete, so a module that is renamed
    and grown past a ceiling in one step is still measured.
    """
    with tempfile.TemporaryFile() as paths_file:
        subprocess.run(
            [
                "git",
                "diff",
                "--diff-filter=AM",
                "--no-renames",
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


def _blob_at(repository: Path, revision: str, relative: str) -> bytes | None:
    """Return the file's bytes at ``revision``, or ``None`` when absent there."""
    completed = subprocess.run(
        ["git", "cat-file", "blob", f"{revision}:{relative}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        return None
    return completed.stdout


def _line_count(payload: bytes) -> int:
    """Count lines the way iterating a file does: only ``\\n`` ends one."""
    return payload.count(b"\n") + (1 if payload and not payload.endswith(b"\n") else 0)


def _exceeded_limits(
    size: int,
    line_count: int | None,
    *,
    max_bytes: int,
    max_py_lines: int,
) -> dict[str, str]:
    """Map each broken ceiling to the phrase describing how it was broken."""
    exceeded: dict[str, str] = {}
    if size > max_bytes:
        exceeded["bytes"] = f"file is {size} bytes, limit is {max_bytes}"
    if line_count is not None and line_count > max_py_lines:
        exceeded["lines"] = f"Python module is {line_count} lines, limit is {max_py_lines}"
    return exceeded


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


def _annotate(severity: str, path: str, message: str) -> None:
    print(
        f"::{severity} file={_command_property(path)}::{_command_message(message)}",
        flush=True,
    )


def _error(path: str, message: str) -> None:
    _annotate("error", path, message)


def _warning(path: str, message: str) -> None:
    _annotate("warning", path, message)


def check_added_files(
    repository: Path,
    base: str,
    head: str,
    *,
    max_bytes: int = MAX_BYTES,
    max_py_lines: int = MAX_PY_LINES,
    require_files: bool = False,
) -> list[Violation]:
    """Return the ceilings this change breaks, tagged with which ceiling each is."""
    violations: list[Violation] = []
    changed_paths = _changed_paths(repository, base, head)
    if require_files and not changed_paths:
        return [
            Violation(
                "repository",
                "repository",
                "no files were available for the complete-tree check",
            )
        ]
    for relative in changed_paths:
        path = repository / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        head_lines = _line_count(path.read_bytes()) if path.suffix == ".py" else None
        broken = _exceeded_limits(
            metadata.st_size,
            head_lines,
            max_bytes=max_bytes,
            max_py_lines=max_py_lines,
        )
        if not broken:
            continue
        previous = _blob_at(repository, base, relative)
        already_broken: dict[str, str] = {}
        if previous is not None:
            already_broken = _exceeded_limits(
                len(previous),
                _line_count(previous) if path.suffix == ".py" else None,
                max_bytes=max_bytes,
                max_py_lines=max_py_lines,
            )
        origin = "added by" if previous is None else "grown past the limit by"
        for limit, description in broken.items():
            if limit in already_broken:
                continue
            violations.append(Violation(limit, relative, f"{description} ({origin} this change)"))
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
    failures = 0
    for violation in violations:
        if violation.limit in ADVISORY_LIMITS:
            _warning(violation.path, violation.message)
            continue
        _error(violation.path, violation.message)
        failures += 1
    if failures:
        return 1
    print("File hygiene checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
