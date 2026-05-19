"""Repo Map — lightweight project topology for system prompt injection.

Eliminates blind exploration: instead of dozens of ls/find/cat calls,
agents get a filtered directory tree in their first message.

Ref:
- openclaw: git-root.ts (git discovery), directory-cli.ts (tree rendering)
- Design doc: 机制一 — 全局拓扑感知, get_repo_map as system supplement
"""

from __future__ import annotations

import os

# Directories always skipped (no config needed — first-principles minimalism)
_ALWAYS_SKIP = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".eggs", ".tox", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".next", ".nuxt", "target",
})


def get_repo_map(
    workspace: str,
    max_depth: int = 3,
    max_entries: int = 80,
) -> str:
    """Generate a lightweight directory tree for system prompt injection.

    Respects .gitignore if `pathspec` is installed; otherwise falls back
    to hardcoded skip list only.  Caps output at max_entries to prevent
    token explosion on large repos.
    """
    workspace = os.path.abspath(workspace)
    gitignore_spec = _load_gitignore(workspace)

    lines: list[str] = [os.path.basename(workspace) + "/"]
    _walk(workspace, "", 0, max_depth, max_entries, lines, gitignore_spec, workspace)

    if len(lines) >= max_entries:
        lines.append(f"... ({max_entries}+ entries, truncated)")

    return "\n".join(lines)


def _walk(
    dirpath: str,
    prefix: str,
    depth: int,
    max_depth: int,
    max_entries: int,
    lines: list[str],
    gitignore_spec,
    root: str,
) -> None:
    if depth >= max_depth or len(lines) >= max_entries:
        return

    try:
        entries = sorted(os.listdir(dirpath))
    except PermissionError:
        return

    # Separate dirs and files, filter
    dirs = []
    files = []
    for name in entries:
        if name.startswith(".") and name in _ALWAYS_SKIP:
            continue
        if name in _ALWAYS_SKIP:
            continue

        full = os.path.join(dirpath, name)
        rel = os.path.relpath(full, root)

        # Check .gitignore
        if gitignore_spec:
            try:
                if gitignore_spec.match_file(rel):
                    continue
            except Exception:
                pass

        if os.path.isdir(full):
            dirs.append(name)
        elif os.path.isfile(full):
            files.append(name)

    items = dirs + files
    for i, name in enumerate(items):
        if len(lines) >= max_entries:
            return

        is_last = (i == len(items) - 1)
        connector = "└── " if is_last else "├── "
        suffix = "/" if name in dirs else ""
        lines.append(f"{prefix}{connector}{name}{suffix}")

        if name in dirs:
            extension = "    " if is_last else "│   "
            _walk(
                os.path.join(dirpath, name),
                prefix + extension,
                depth + 1,
                max_depth,
                max_entries,
                lines,
                gitignore_spec,
                root,
            )


def _load_gitignore(workspace: str):
    """Try loading .gitignore via pathspec. Returns None if unavailable."""
    gitignore_path = os.path.join(workspace, ".gitignore")
    if not os.path.isfile(gitignore_path):
        return None
    try:
        import pathspec
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            return pathspec.PathSpec.from_lines("gitwildmatch", f)
    except ImportError:
        return None
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    print(get_repo_map(path))
