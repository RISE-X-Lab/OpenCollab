"""Workflow source names preserve ordinary sibling and self imports."""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli.main import app
from opencollab.bootstrap import _workflow_runtime_discovery as workflow_discovery


def _import_state():
    return (
        {name for name in sys.modules if name.startswith("_opencollab_workflow_")},
        dict(workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots),
    )


def _write_sibling_workflow(workspace, *, entry, sibling):
    directory = workspace / "workflows"
    directory.mkdir()
    (directory / f"{sibling}.py").write_text(
        'def shared_value():\n    return "ordinary sibling import"\n',
        encoding="utf-8",
    )
    (directory / f"{entry}.py").write_text(
        "from opencollab import workflow\n"
        f"from .{sibling} import shared_value\n"
        '@workflow(name="sibling-import")\n'
        "async def run(ctx, args):\n"
        "    return shared_value()\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "entry,sibling",
    [("task", "workflow"), ("task", "helpers"), ("workflow", "helpers"),
     ("task.v1", "workflow"), ("task.v1", "helpers")],
)
def test_cli_lists_workflow_with_ordinary_sibling_import(tmp_path, monkeypatch, entry, sibling):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    _write_sibling_workflow(tmp_path, entry=entry, sibling=sibling)
    before = _import_state()

    result = CliRunner().invoke(app, ["workflow", "list", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.exception
    assert "sibling-import" in result.stdout
    assert _import_state() == before


@pytest.mark.parametrize(
    "entry,sibling",
    [("task", "workflow"), ("task", "helpers"), ("workflow", "helpers"),
     ("task.v1", "workflow"), ("task.v1", "helpers")],
)
async def test_sdk_runs_workflow_with_ordinary_sibling_import(tmp_path, monkeypatch, entry, sibling):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    _write_sibling_workflow(tmp_path, entry=entry, sibling=sibling)
    before = _import_state()
    client = OpenCollab(tmp_path, config={"model": "test-model"})

    result = await client.workflow("sibling-import", trace=False)

    assert result.status == "completed", result.reason
    assert result.output == "ordinary sibling import"
    assert _import_state() == before


async def test_multiple_workflow_files_keep_sibling_and_self_imports(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "workflow.py").write_text(
        "from opencollab import workflow\n"
        "def shared_value():\n"
        '    return "shared"\n'
        '@workflow(name="original-entry")\n'
        "async def original(ctx, args):\n"
        "    from .task import task_value\n"
        '    return shared_value() + " " + task_value()\n',
        encoding="utf-8",
    )
    (directory / "task.py").write_text(
        "from opencollab import workflow\n"
        "from .workflow import original, shared_value\n"
        "def task_value():\n"
        '    return shared_value() + " task"\n'
        '@workflow(name="task-entry")\n'
        "async def task(ctx, args):\n"
        "    from .task import task_value\n"
        "    return task_value()\n",
        encoding="utf-8",
    )
    before = _import_state()

    registry = workflow_discovery.discover_workflows(str(directory))

    assert [spec.name for spec in registry.list_specs()] == ["original-entry", "task-entry"]
    assert _import_state() == before
    for _ in range(2):
        assert await registry.get("original-entry").fn(None, {}) == "shared shared task"
        assert _import_state() == before
        assert await registry.get("task-entry").fn(None, {}) == "shared task"
        assert _import_state() == before
