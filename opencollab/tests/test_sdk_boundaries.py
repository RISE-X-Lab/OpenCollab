"""Dependency gates for the public SDK and evaluation integrations."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "opencollab" / "opencollab"
_INTERNAL_OC_PREFIXES = (
    "opencollab.adapters",
    "opencollab.application",
    "opencollab.bootstrap",
    "opencollab.domain",
)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_external_workflows_only_use_the_public_sdk() -> None:
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "workflows").glob("*.py")):
        for imported in _imports(path):
            if imported.startswith("opencollab.") and not imported.startswith(
                "opencollab.sdk"
            ):
                offenders.append(f"{path.name}: {imported}")
    assert offenders == []


def test_migratable_eval_integrations_do_not_import_oc_internals() -> None:
    paths = (
        _PACKAGE_ROOT / "harness" / "eval_adapter" / "workspace.py",
        _PACKAGE_ROOT / "harness" / "workflow_backend.py",
        _REPO_ROOT / "scripts" / "swe_eval_run.py",
    )
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {imported}"
        for path in paths
        for imported in _imports(path)
        if imported.startswith(_INTERNAL_OC_PREFIXES)
    ]
    assert offenders == []


def test_sdk_does_not_depend_on_evaluation_implementations() -> None:
    offenders = [
        f"{path.name}: {imported}"
        for path in sorted((_PACKAGE_ROOT / "sdk").glob("*.py"))
        for imported in _imports(path)
        if imported.startswith("opencollab.harness")
        or imported == "swebench"
        or imported.startswith("swebench.")
    ]
    assert offenders == []
