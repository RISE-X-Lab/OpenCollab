"""Dependency gates for the public SDK and evaluation integrations."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "opencollab" / "opencollab"


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_sdk_does_not_depend_on_evaluation_implementations() -> None:
    offenders = [
        f"{path.name}: {imported}"
        for path in sorted((_PACKAGE_ROOT / "sdk").glob("*.py"))
        for imported in _imports(path)
        if imported.startswith("opencollab.harness")
        or imported == "opencollab_eval"
        or imported.startswith("opencollab_eval.")
        or imported == "swebench"
        or imported.startswith("swebench.")
    ]
    assert offenders == []
