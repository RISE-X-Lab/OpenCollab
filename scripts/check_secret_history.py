#!/usr/bin/env python3
"""Scan Git trees with an explicitly trusted secret baseline."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

BASELINE_PATH = ".secrets.baseline"


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout


def _commits(repository: Path, base: str, head: str) -> list[str]:
    output = _git(repository, "rev-list", "--reverse", f"{base}..{head}")
    commits = output.decode("ascii").split()
    return commits or [head]


def _tree_blobs(repository: Path, commit: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw_entry in _git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        _mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        relative = os.fsdecode(raw_path)
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError(f"unsafe Git path: {relative!r}")
        entries.append((relative, object_id))
    return entries


def _materialize_tree(repository: Path, commit: str, destination: Path) -> list[str]:
    paths: list[str] = []
    for relative, object_id in _tree_blobs(repository, commit):
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git(repository, "cat-file", "blob", object_id))
        paths.append(relative)
    return paths


def _scan_materialized_tree(
    repository: Path,
    commit: str,
    scanner: list[str],
    trusted_baseline: bytes,
    root: Path,
) -> int:
    tree = root / commit
    tree.mkdir()
    paths = [
        path
        for path in _materialize_tree(repository, commit, tree)
        if path != BASELINE_PATH
    ]
    if not paths:
        print(f"::error::Proposed commit {commit} has no files to scan.")
        return 1

    _git(tree, "init", "--quiet")
    baseline = tree / BASELINE_PATH
    baseline.write_bytes(trusted_baseline)
    completed = subprocess.run(
        [
            *scanner,
            "--no-verify",
            "--baseline",
            BASELINE_PATH,
            "--",
            *paths,
        ],
        cwd=tree,
        check=False,
    )
    if completed.returncode:
        print(f"::error::Secret scan failed for proposed commit {commit}.")
    return completed.returncode


def check_secret_history(
    repository: Path,
    base: str,
    head: str,
    scanner: list[str],
    *,
    allow_baseline_change: bool = False,
) -> int:
    baseline_changed = subprocess.run(
        ["git", "diff", "--quiet", base, head, "--", BASELINE_PATH],
        cwd=repository,
        check=False,
    )
    if baseline_changed.returncode not in (0, 1):
        raise subprocess.CalledProcessError(baseline_changed.returncode, baseline_changed.args)
    if baseline_changed.returncode == 1:
        changed = {
            os.fsdecode(path)
            for path in _git(repository, "diff", "--name-only", "-z", base, head).split(b"\0")
            if path
        }
        if not allow_baseline_change or changed != {BASELINE_PATH}:
            print(
                "::error file=.secrets.baseline::Secret baseline changes require "
                "a baseline-only PR with the security-baseline-update label."
            )
            return 1

    trusted_baseline = _git(repository, "show", f"{base}:{BASELINE_PATH}")
    with tempfile.TemporaryDirectory(prefix="opencollab-secret-history-") as temporary:
        root = Path(temporary)
        for commit in _commits(repository, base, head):
            result = _scan_materialized_tree(
                repository,
                commit,
                scanner,
                trusted_baseline,
                root,
            )
            if result:
                return result
    print("Secret history checks passed.")
    return 0


def check_trusted_tree(repository: Path, commit: str, scanner: list[str]) -> int:
    """Scan a trusted branch tree using the audited baseline in that tree."""
    commit = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii").strip()
    trusted_baseline = _git(repository, "show", f"{commit}:{BASELINE_PATH}")
    with tempfile.TemporaryDirectory(prefix="opencollab-secret-tree-") as temporary:
        result = _scan_materialized_tree(
            repository,
            commit,
            scanner,
            trusted_baseline,
            Path(temporary),
        )
    if not result:
        print("Trusted tree secret check passed.")
    return result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?")
    parser.add_argument("head", nargs="?")
    parser.add_argument(
        "--trusted-tree",
        help="Scan a trusted branch tree using its own audited baseline.",
    )
    parser.add_argument(
        "--scanner-command",
        default="detect-secrets-hook",
        help="Command used to scan one materialized tree.",
    )
    parser.add_argument(
        "--allow-baseline-change",
        action="store_true",
        help="Allow a baseline-only change that a maintainer explicitly labeled.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        scanner = shlex.split(args.scanner_command)
        if args.trusted_tree:
            if args.base or args.head or args.allow_baseline_change:
                raise ValueError("--trusted-tree cannot be combined with history arguments")
            return check_trusted_tree(Path.cwd(), args.trusted_tree, scanner)
        if not args.base or not args.head:
            raise ValueError("base and head are required for a proposed-history scan")
        return check_secret_history(
            Path.cwd(),
            args.base,
            args.head,
            scanner,
            allow_baseline_change=args.allow_baseline_change,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"::error::Secret history preparation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
