"""Installed workflow discovery through the CLI and public SDK."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli import workflow as workflow_cli
from opencollab.bootstrap.programmatic import ProgrammaticResult
from opencollab.bootstrap.workflow_runtime import discover_workflows
from opencollab.builtin_workflows import duo, get_builtin_workflows
from opencollab.sdk import client as sdk_client
from opencollab.workflows import workflow

_BUILTIN_NAMES = {
    "duo", "duo-v3",
    "validation-council-dual-coder-selection-v2",
    "validation-council-dual-coder-selection-v3",
}


def _write_workflow(directory, *, name="local-flow"):
    directory.mkdir(parents=True)
    (directory / "local.py").write_text(
        "from opencollab.workflows import workflow\n"
        f"@workflow(name={name!r})\n"
        "async def local(ctx, args):\n"
        "    return args\n",
        encoding="utf-8",
    )


def test_builtin_registry_is_fresh_and_contains_both_compatibility_names():
    first = get_builtin_workflows()
    second = get_builtin_workflows()
    assert {spec.name for spec in first.list_specs()} == _BUILTIN_NAMES
    assert second.get("duo").fn is duo

    @workflow(name="caller-only")
    async def caller(ctx, args):
        return args

    first.register(caller.__workflow_spec__)
    with pytest.raises(KeyError):
        second.get("caller-only")


def test_directory_discovery_keeps_caller_only_default_and_can_include_builtins(tmp_path):
    directory = tmp_path / "workflows"
    _write_workflow(directory)
    caller = discover_workflows(str(directory))
    combined = discover_workflows(str(directory), include_builtin=True)
    assert [spec.name for spec in caller.list_specs()] == ["local-flow"]
    assert {spec.name for spec in combined.list_specs()} == _BUILTIN_NAMES | {"local-flow"}
    assert discover_workflows(str(tmp_path / "missing")).list_specs() == []
    installed = discover_workflows(str(tmp_path / "missing"), include_builtin=True)
    assert {spec.name for spec in installed.list_specs()} == _BUILTIN_NAMES


@pytest.mark.parametrize("name", sorted(_BUILTIN_NAMES))
def test_caller_module_using_builtin_name_raises_existing_duplicate_error(tmp_path, name):
    directory = tmp_path / "workflows"
    _write_workflow(directory, name=name)
    with pytest.raises(ValueError, match="workflow name already registered"):
        discover_workflows(str(directory), include_builtin=True)


def test_cli_lists_installed_duo_in_workspace_without_workflow_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    result = CliRunner().invoke(workflow_cli.app, ["list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "duo" in result.stdout
    assert "duo-v3" in result.stdout


def test_cli_runs_installed_duo_and_forwards_agent_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    monkeypatch.setattr(workflow_cli, "missing_api_key_for", lambda *args: False)
    captured = {}

    async def run(spec, args, **kwargs):
        captured.update(spec=spec, args=args, **kwargs)
        return {"status": "done"}

    monkeypatch.setattr(workflow_cli, "run_workflow", run)
    result = CliRunner().invoke(
        workflow_cli.app,
        ["run", "duo", "--workspace", str(tmp_path), "--args", '{"goal":"repair"}',
         "--agent-profile", "single2", "--no-save"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "done"}
    assert captured["spec"].fn is duo
    assert captured["args"] == {"goal": "repair"}
    assert captured["agent_profile"] == "single2"


@pytest.mark.parametrize("name", sorted(_BUILTIN_NAMES))
async def test_sdk_name_resolves_installed_workflow_and_preserves_execution_options(tmp_path, monkeypatch, name):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    captured = {}

    async def run(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(output={"status": "done"}, status="completed", reason=None, tokens=7, artifacts=None)

    monkeypatch.setattr(sdk_client, "run_workflow", run)
    environment = object()
    candidate_workspace = object()
    result = await OpenCollab(tmp_path, environment=environment).workflow(
        name, {"goal": "repair"}, agent_profile="single2", budget=9000,
        concurrency=2, task_concurrency=3, candidate_workspace=candidate_workspace,
        trace=False,
    )
    assert result.ok
    assert captured["workflow"].name == name
    assert captured["inputs"] == {"goal": "repair"}
    assert captured["environment"] is environment
    assert captured["candidate_workspace"] is candidate_workspace
    assert captured["max_tokens"] == 9000
    assert captured["max_concurrency"] == 2
    assert captured["task_concurrency"] == 3
    assert captured["agent_profile"].name == "single2"


@pytest.mark.parametrize("absolute", [False, True])
async def test_sdk_name_resolves_caller_directory_from_workspace(tmp_path, monkeypatch, absolute):
    project = tmp_path / "project"
    directory = project / "custom"
    _write_workflow(directory)
    monkeypatch.setenv("OPENCOLLAB_WORKFLOWS_DIR", str(directory) if absolute else "custom")
    monkeypatch.chdir(tmp_path)
    result = await OpenCollab(project).workflow("local-flow", {"value": 19}, trace=False)
    assert result.ok and result.output == {"value": 19}


async def test_sdk_unknown_name_and_builtin_conflict_fail_before_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    client = OpenCollab(tmp_path)
    with pytest.raises(ValueError, match="unknown workflow.*missing.*duo"):
        await client.workflow("missing")
    _write_workflow(tmp_path / "workflows", name="duo")
    with pytest.raises(ValueError, match="workflow name already registered"):
        await client.workflow("duo")


@pytest.mark.parametrize("use_spec", [False, True])
async def test_sdk_explicit_function_or_spec_bypasses_name_discovery(tmp_path, monkeypatch, use_spec):
    _write_workflow(tmp_path / "workflows", name="duo")

    @workflow(name="duo")
    async def custom(ctx, args):
        return args

    flow = custom.__workflow_spec__ if use_spec else custom
    result = await OpenCollab(tmp_path).workflow(flow, {"caller": True}, trace=False)
    assert result.ok and result.output == {"caller": True}
