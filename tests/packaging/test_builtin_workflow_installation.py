"""Built-in workflows execute from the package without example workspaces."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from tests.support.paths import REPO_ROOT


def test_builtin_modules_import_and_execute_from_package_only_tree(tmp_path):
    package_root = tmp_path / "installed"
    shutil.copytree(
        REPO_ROOT / "opencollab", package_root / "opencollab",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    assert not (package_root / "examples").exists()
    source = """
import asyncio
import importlib
import importlib.abc
import json
import pkgutil
import runpy
import sys
from pathlib import Path

package_root = Path(sys.argv[1]).resolve()
checkout_root = Path(sys.argv[2]).resolve()
sys.path = [entry for entry in sys.path if Path(entry).resolve() != checkout_root]
sys.path.insert(0, str(package_root))

class ExampleImportsUnavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "examples" or fullname.startswith("examples."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, ExampleImportsUnavailable())
import opencollab.builtin_workflows as builtins
assert Path(builtins.__file__).resolve().is_relative_to(package_root)
modules = sorted(info.name for info in pkgutil.walk_packages(
    builtins.__path__, builtins.__name__ + ".",
))
for name in modules:
    imported = importlib.import_module(name)
    assert Path(imported.__file__).resolve().is_relative_to(package_root), name
names = sorted(spec.name for spec in builtins.get_builtin_workflows().list_specs())
assert names == ["duo", "evolution"], names
workspace = package_root.parent / "ordinary-task"
workspace.mkdir()
probe = runpy.run_path(sys.argv[3])
execution = asyncio.run(probe["exercise_evolution"](workspace))
print(json.dumps({"modules": modules, "workflows": names, "execution": execution}))
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", source, str(package_root), str(REPO_ROOT),
         str(REPO_ROOT / "tests/support/installed_evolution_smoke.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    report = json.loads(completed.stdout)
    assert "opencollab.builtin_workflows.evolution" in report["modules"]
    assert report["workflows"] == ["duo", "evolution"]
    assert report["execution"]["content"] == "alpha\nbeta\n"
    assert report["execution"]["delivery_ok"] is True
