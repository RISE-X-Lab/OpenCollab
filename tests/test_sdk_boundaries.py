"""Dependency and surface gates for the compact public API."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "opencollab"


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_public_api_delegates_to_bootstrap_without_concrete_imports() -> None:
    public_files = [
        *_PACKAGE_ROOT.joinpath("sdk").glob("*.py"),
        _PACKAGE_ROOT / "tools.py",
        _PACKAGE_ROOT / "environments.py",
        _PACKAGE_ROOT / "workflows.py",
    ]
    offenders = [
        f"{path.name}: {imported}"
        for path in sorted(public_files)
        for imported in _imports(path)
        if imported.startswith("opencollab.adapters")
        or imported.startswith("opencollab.domain")
    ]
    assert offenders == []


def test_public_api_does_not_depend_on_evaluation_implementations() -> None:
    public_files = [
        *_PACKAGE_ROOT.joinpath("sdk").glob("*.py"),
        _PACKAGE_ROOT / "tools.py",
        _PACKAGE_ROOT / "environments.py",
        _PACKAGE_ROOT / "workflows.py",
    ]
    forbidden = ("opencollab.harness", "opencollab_eval", "swebench")
    offenders = [
        f"{path.name}: {imported}"
        for path in sorted(public_files)
        for imported in _imports(path)
        if imported.startswith(forbidden)
    ]
    assert offenders == []


def test_v2_request_dto_modules_stay_deleted() -> None:
    sdk_root = _PACKAGE_ROOT / "sdk"
    retired = {
        "agents",
        "config",
        "environment",
        "environments",
        "errors",
        "eval_compat",
        "experimental",
        "lifecycle",
        "models",
        "persistence",
        "repository",
        "runtime",
        "tools",
        "tracing",
        "usage",
        "workflows",
    }
    assert not {path.stem for path in sdk_root.glob("*.py")} & retired


def test_public_modules_export_only_documented_names() -> None:
    import opencollab.environments as environments
    import opencollab.sdk as sdk
    import opencollab.tools as tools
    import opencollab.workflows as workflows

    for module in (sdk, tools, environments, workflows):
        assert module.__all__
        assert all(not name.startswith("_") for name in module.__all__)
