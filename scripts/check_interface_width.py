#!/usr/bin/env python3
"""Report the public top-level surface of every module.

A good split reduces the total number of public names; a split done only to
satisfy a line ceiling increases it. This measures the thing that matters.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Set one name above the widest module in the tree, so the ceiling actually
# bites: a limit nothing can reach is not a gate. Lower it as the tree narrows.
MAX_WIDTH = 20
EXEMPT = {"__init__.py", "ports.py"}


def width(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names += not node.name.startswith("_")
        elif isinstance(node, ast.Assign):
            names += sum(
                isinstance(t, ast.Name) and not t.id.startswith("_")
                for t in node.targets
            )
    return names


def main(root: str = "opencollab") -> int:
    failed = False
    for path in sorted(Path(root).rglob("*.py")):
        if path.name in EXEMPT or "__pycache__" in path.parts:
            continue
        n = width(path)
        if n > MAX_WIDTH:
            print(f"::warning file={path}::public surface is {n} names, limit is {MAX_WIDTH}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
