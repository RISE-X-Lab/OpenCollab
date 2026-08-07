"""Repo-map generation — a bounded directory sketch for agent orientation.

Mid-tier backends burn early context discovering the repo layout (``ls``,
``find``, reading the wrong files). A startup repo map answers that once, at
zero per-call schema cost. ``build_repo_map`` walks a local workspace;
``build_repo_map_via_env`` asks an ``Environment`` (so it also works for a
Docker workspace) with a single ``find``. Both return ``""`` when nothing
useful can be produced — callers inject the map only when non-empty.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from opencollab.adapters.env import Environment
from opencollab.adapters.tools._output import truncate

MAX_MAP_CHARS = 4_000
MAX_MAP_DEPTH = 3
MAX_MAP_ENTRIES = 300
MAX_MAP_DIRS = 100
MAX_ENTRIES_PER_DIR = 30
# Bulky generated/vendored dirs that never help orientation. Hidden entries
# (".git", ".venv", ...) are skipped by their dot prefix.
SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules", "dist", "build", "venv"})
ROOT_PRIORITY_FILES = frozenset(
    {
        "Cargo.toml",
        "README",
        "README.md",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "team.yaml",
        "team.yml",
    }
)

MAP_HEADER = "## Repository layout"
_TRUNCATED_MARKER = "... (repository map truncated; traversal budget reached)"


def _keep(name: str) -> bool:
    return not name.startswith(".") and name not in SKIP_DIR_NAMES


def build_repo_map(
    workspace: str,
    *,
    max_depth: int = MAX_MAP_DEPTH,
    max_chars: int = MAX_MAP_CHARS,
    max_entries: int = MAX_MAP_ENTRIES,
    max_dirs: int = MAX_MAP_DIRS,
    max_scanned_entries: int | None = None,
) -> str:
    """A depth- and size-bounded tree of ``workspace``, or ``""``."""
    if not workspace or not os.path.isdir(workspace):
        return ""
    scanned_limit = (
        max_scanned_entries
        if max_scanned_entries is not None
        else max(MAX_ENTRIES_PER_DIR + 2, max_entries * 4)
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (
            max_depth,
            max_chars,
            max_entries,
            max_dirs,
            scanned_limit,
        )
    ):
        raise ValueError("repo-map budgets must be positive integers")
    budget = _WalkBudget(
        max_depth=max_depth,
        max_chars=max_chars,
        max_entries=max_entries,
        max_dirs=max_dirs,
        max_scanned_entries=scanned_limit,
    )
    _walk(workspace, 0, budget)
    if not budget.lines and not budget.truncated:
        return ""
    return _render_map(budget.lines, max_chars=max_chars, truncated=budget.truncated)


@dataclass(slots=True)
class _WalkBudget:
    max_depth: int
    max_chars: int
    max_entries: int
    max_dirs: int
    max_scanned_entries: int
    lines: list[str] = field(default_factory=list)
    shown_entries: int = 0
    scanned_entries: int = 0
    scanned_dirs: int = 0
    body_chars: int = 0
    truncated: bool = False

    def append(self, line: str) -> bool:
        separator = 1 if self.lines else 0
        if (
            self.shown_entries >= self.max_entries
            or self.body_chars + separator + len(line) > self.max_chars
        ):
            self.truncated = True
            return False
        self.lines.append(line)
        self.shown_entries += 1
        self.body_chars += separator + len(line)
        return True


def _render_map(lines: list[str], *, max_chars: int, truncated: bool) -> str:
    body = "\n".join(lines)
    if truncated:
        body = f"{body}\n{_TRUNCATED_MARKER}" if body else _TRUNCATED_MARKER
    return f"{MAP_HEADER}\n\n" + truncate(body, max_chars)


def _walk(path: str, depth: int, budget: _WalkBudget) -> None:
    if budget.truncated:
        return
    if budget.scanned_dirs >= budget.max_dirs:
        budget.truncated = True
        return
    budget.scanned_dirs += 1
    candidates: list[os.DirEntry[str]] = []
    kept_entries = 0
    completed_scan = True
    try:
        with os.scandir(path) as it:
            iterator = iter(it)
            while budget.scanned_entries < budget.max_scanned_entries:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                budget.scanned_entries += 1
                if not _keep(entry.name):
                    continue
                kept_entries += 1
                entry_key = (
                    not (
                        depth == 0
                        and entry.name in ROOT_PRIORITY_FILES
                        and not entry.is_dir(follow_symlinks=False)
                    ),
                    not entry.is_dir(follow_symlinks=False),
                    entry.name,
                )
                if len(candidates) < MAX_ENTRIES_PER_DIR:
                    candidates.append(entry)
                else:
                    worst_index = max(
                        range(len(candidates)),
                        key=lambda index: (
                            not (
                                depth == 0
                                and candidates[index].name in ROOT_PRIORITY_FILES
                                and not candidates[index].is_dir(follow_symlinks=False)
                            ),
                            not candidates[index].is_dir(follow_symlinks=False),
                            candidates[index].name,
                        ),
                    )
                    worst = candidates[worst_index]
                    worst_key = (
                        not (
                            depth == 0
                            and worst.name in ROOT_PRIORITY_FILES
                            and not worst.is_dir(follow_symlinks=False)
                        ),
                        not worst.is_dir(follow_symlinks=False),
                        worst.name,
                    )
                    if entry_key < worst_key:
                        candidates[worst_index] = entry
            else:
                budget.truncated = True
                completed_scan = False
    except OSError:
        return
    candidates.sort(
        key=lambda entry: (
            not (
                depth == 0
                and entry.name in ROOT_PRIORITY_FILES
                and not entry.is_dir(follow_symlinks=False)
            ),
            not entry.is_dir(follow_symlinks=False),
            entry.name,
        )
    )
    indent = "  " * depth
    for entry in candidates:
        if entry.is_dir(follow_symlinks=False):
            if not budget.append(f"{indent}{entry.name}/"):
                return
            if depth + 1 < budget.max_depth:
                _walk(entry.path, depth + 1, budget)
        else:
            if not budget.append(f"{indent}{entry.name}"):
                return
    omitted = kept_entries - len(candidates)
    if completed_scan and omitted > 0:
        budget.append(f"{indent}... ({omitted} more)")


async def build_repo_map_via_env(
    env: Environment,
    *,
    max_depth: int = MAX_MAP_DEPTH,
    max_chars: int = MAX_MAP_CHARS,
    max_entries: int = MAX_MAP_ENTRIES,
) -> str:
    """A bounded path listing of the environment's workspace, or ``""``.

    Uses one ``find`` so it works wherever the workspace actually lives
    (e.g. inside a Docker container with no local mount).
    """
    prunes = " -o ".join(
        ["-name '.*'"] + [f"-name '{name}'" for name in sorted(SKIP_DIR_NAMES)]
    )
    # -mindepth 1 keeps "." itself out of the prune tests — without it the
    # '.*' pattern matches the root and prunes the entire tree.
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (max_depth, max_chars, max_entries)
    ):
        raise ValueError("repo-map budgets must be positive integers")
    cmd = (
        rf"find . -mindepth 1 -maxdepth {max_depth} "
        rf"\( {prunes} \) -prune -o -print | head -n {max_entries + 1}"
    )
    try:
        result = await env.exec_cmd(cmd)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    paths = [
        line[2:] if line.startswith("./") else line
        for line in result.stdout.splitlines()
        if line.strip() and line.strip() != "."
    ]
    if not paths:
        return ""
    truncated = len(paths) > max_entries or result.stdout_truncated
    return _render_map(
        paths[:max_entries],
        max_chars=max_chars,
        truncated=truncated,
    )


__all__ = [
    "MAP_HEADER",
    "MAX_MAP_CHARS",
    "MAX_MAP_DEPTH",
    "MAX_MAP_DIRS",
    "MAX_MAP_ENTRIES",
    "build_repo_map",
    "build_repo_map_via_env",
]
