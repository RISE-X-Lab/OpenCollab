"""Workflow source names preserve ordinary sibling and self imports."""

from __future__ import annotations

import asyncio
import sys

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli.main import app
from opencollab.application.workflow import WorkflowContext
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


async def test_concurrent_calls_share_one_loaded_package_until_both_finish(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "_helper.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (directory / "task.py").write_text(
        "from opencollab import workflow\n"
        "@workflow\n"
        "async def overlap(ctx, args):\n"
        "    args['entered'].set()\n"
        "    await args['release'].wait()\n"
        "    from ._helper import answer\n"
        "    return answer()\n",
        encoding="utf-8",
    )
    before = _import_state()
    spec = workflow_discovery.discover_workflows(str(directory)).get("overlap")
    client = OpenCollab(tmp_path, model="offline")
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = [asyncio.create_task(client.workflow(
        spec, {"entered": entered[index], "release": release[index]}, trace=False,
    )) for index in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 5)
        release[0].set()
        assert (await asyncio.wait_for(calls[0], 5)).output == 42
        assert _import_state() != before
        release[1].set()
        assert (await asyncio.wait_for(calls[1], 5)).output == 42
        assert _import_state() == before
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.parametrize("awaited_first", [False, True])
async def test_cleanup_callbacks_keep_imports_for_following_owned_tasks(tmp_path, awaited_first):
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "_helper.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (directory / "task.py").write_text('''import asyncio
from opencollab import workflow

@workflow
async def cleanup(ctx, args):
    async def finish():
        args["following_started"].set()
        await args["release_following"].wait()
        from ._helper import answer
        args["answers"].append(answer())

    def follow(_task):
        asyncio.get_running_loop().call_soon(
            lambda: ctx._track_pending_cleanup(asyncio.create_task(finish())),
        )

    first = asyncio.create_task(asyncio.sleep(0))
    first.add_done_callback(follow)
    ctx._track_pending_cleanup(first)
    if args["awaited_first"]:
        await first
    return "returned"
''', encoding="utf-8")
    before = _import_state()
    spec = workflow_discovery.discover_workflows(str(directory)).get("cleanup")
    ctx = WorkflowContext(factory=object())
    args = {name: asyncio.Event() for name in (
        "following_started", "release_following",
    )}
    args["awaited_first"] = awaited_first
    args["answers"] = []
    caller = asyncio.current_task()
    ctx._active_call_tasks.add(caller)
    try:
        assert await spec.fn(ctx, args) == "returned"
        assert _import_state() != before
        await asyncio.wait_for(args["following_started"].wait(), 5)
        assert _import_state() != before
        args["release_following"].set()
        await asyncio.wait_for(ctx.wait_for_pending_cleanup(), 5)
        assert args["answers"] == [42]
        assert _import_state() == before
    finally:
        args["release_following"].set()
        ctx._active_call_tasks.discard(caller)
        await ctx.wait_for_pending_cleanup()
