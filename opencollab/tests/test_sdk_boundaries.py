"""Dependency and surface gates for the public SDK."""

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


def test_retired_sdk_compatibility_modules_are_absent() -> None:
    sdk_root = _PACKAGE_ROOT / "sdk"
    assert not (sdk_root / "eval_compat.py").exists()
    assert not (sdk_root / "experimental.py").exists()


def test_sdk_capability_modules_export_only_public_names() -> None:
    import opencollab.sdk as sdk

    sdk_root = _PACKAGE_ROOT / "sdk"
    for path in sorted(sdk_root.glob("*.py")):
        if path.name == "__init__.py":
            module = sdk
        else:
            module = __import__(f"opencollab.sdk.{path.stem}", fromlist=["__all__"])
        exports = getattr(module, "__all__", ())
        assert all(not name.startswith("_") for name in exports), path.name
