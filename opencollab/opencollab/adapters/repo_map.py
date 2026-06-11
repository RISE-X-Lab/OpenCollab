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

from opencollab.adapters.env import Environment
from opencollab.adapters.tools._output import truncate

MAX_MAP_CHARS = 4_000
MAX_MAP_DEPTH = 3
MAX_ENTRIES_PER_DIR = 30
# Bulky generated/vendored dirs that never help orientation. Hidden entries
# (".git", ".venv", ...) are skipped by their dot prefix.
SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules", "dist", "build", "venv"})

MAP_HEADER = "## Repository layout"


def _keep(name: str) -> bool:
    return not name.startswith(".") and name not in SKIP_DIR_NAMES


def build_repo_map(
    workspace: str,
    *,
    max_depth: int = MAX_MAP_DEPTH,
    max_chars: int = MAX_MAP_CHARS,
) -> str:
    """A depth- and size-bounded tree of ``workspace``, or ``""``."""
    if not workspace or not os.path.isdir(workspace):
        return ""
    lines: list[str] = []
    _walk(workspace, 0, max_depth, lines)
    if not lines:
        return ""
    return f"{MAP_HEADER}\n\n" + truncate("\n".join(lines), max_chars)


def _walk(path: str, depth: int, max_depth: int, lines: list[str]) -> None:
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: (not e.is_dir(), e.name))
    except OSError:
        return
    entries = [e for e in entries if _keep(e.name)]
    shown = entries[:MAX_ENTRIES_PER_DIR]
    indent = "  " * depth
    for entry in shown:
        if entry.is_dir(follow_symlinks=False):
            lines.append(f"{indent}{entry.name}/")
            if depth + 1 < max_depth:
                _walk(entry.path, depth + 1, max_depth, lines)
        else:
            lines.append(f"{indent}{entry.name}")
    if len(entries) > len(shown):
        lines.append(f"{indent}... ({len(entries) - len(shown)} more)")


async def build_repo_map_via_env(
    env: Environment,
    *,
    max_depth: int = MAX_MAP_DEPTH,
    max_chars: int = MAX_MAP_CHARS,
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
    cmd = (
        rf"find . -mindepth 1 -maxdepth {max_depth} "
        rf"\( {prunes} \) -prune -o -print | sort"
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
    return f"{MAP_HEADER}\n\n" + truncate("\n".join(paths), max_chars)


__all__ = [
    "MAP_HEADER",
    "MAX_MAP_CHARS",
    "MAX_MAP_DEPTH",
    "build_repo_map",
    "build_repo_map_via_env",
]
