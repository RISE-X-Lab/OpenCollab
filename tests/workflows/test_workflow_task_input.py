"""Workflow task input through the CLI and its validation errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from opencollab.adapters.cli import workflow as workflow_cli
from opencollab.application.workflow_registry import Registry, workflow
from tests.support.paths import PACKAGE_ROOT

_TASK = ' 修复 "quoted" 和 \'single\' \\ path\n第二行 Grüße 🌍\n '


@pytest.fixture
def workflow_project(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    config = tmp_path / "configs"
    config.mkdir()
    (config / ".env").write_text("OPENCOLLAB_API_KEY=fixture-key\n", encoding="utf-8")  # pragma: allowlist secret
    (directory / "input_probe.py").write_text(
        "from opencollab.workflows import workflow\n"
        "@workflow(name='input-probe')\n"
        "async def input_probe(ctx, args):\n"
        "    return args\n",
        encoding="utf-8",
    )
    return tmp_path


def _cli(project: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "opencollab", "workflow", "run", "input-probe", *arguments, "--no-save"],
        cwd=project,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(PACKAGE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "240",
            "TERMINAL_WIDTH": "240",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("source", ["text", "file"])
def test_real_cli_task_preserves_quotes_multiline_and_unicode(workflow_project, source):
    other_args = {"task": "custom parameter", "nested": {"attempts": 2}, "allow_unisolated_shell": False}
    if source == "file":
        task_file = workflow_project / "任务 input.txt"
        task_file.write_bytes(_TASK.encode("utf-8"))
        task_options = ["--task-file", str(task_file)]
    else:
        task_options = ["--task", _TASK]

    result = _cli(workflow_project, [*task_options, "--args", json.dumps(other_args)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {**other_args, "goal": _TASK}


def test_real_cli_keeps_existing_args_compatible(workflow_project):
    original_args = {"goal": _TASK, "task": "caller field", "attempts": 2, "allow_unisolated_shell": True}

    result = _cli(workflow_project, ["--args", json.dumps(original_args)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == original_args


def test_duo_task_forwards_goal_and_explicit_shell_choice(tmp_path, monkeypatch):
    captured = {}

    async def run(spec, args, **kwargs):
        captured.update(name=spec.name, args=args)
        return args

    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    monkeypatch.setattr(workflow_cli, "run_workflow", run)
    monkeypatch.setattr(
        workflow_cli,
        "resolve_config",
        lambda *args: {
            "provider": "openai",
            "api_key": "fixture-key",  # pragma: allowlist secret
            "base_url": None,
            "budget": 20_000,
        },
    )
    result = CliRunner().invoke(
        workflow_cli.app,
        ["run", "duo", "--workspace", str(tmp_path), "--task", _TASK,
         "--args", '{"allow_unisolated_shell":true}', "--no-save"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"name": "duo", "args": {"goal": _TASK, "allow_unisolated_shell": True}}


@pytest.fixture
def validation_cli(monkeypatch):
    @workflow(name="input-probe")
    async def input_probe(ctx, args):
        return args

    registry = Registry()
    registry.register(input_probe.__workflow_spec__)
    monkeypatch.setattr(workflow_cli, "load_registry", lambda workspace: registry)

    def reject_configuration(*args):
        raise AssertionError("Invalid task input reached configuration")

    monkeypatch.setattr(workflow_cli, "resolve_config", reject_configuration)

    def invoke(arguments):
        return CliRunner().invoke(workflow_cli.app, ["run", "input-probe", *arguments, "--no-save"])

    return invoke


@pytest.mark.parametrize("task", ["", " ", "\t\n"])
def test_task_rejects_blank_text(validation_cli, task):
    result = validation_cli(["--task", task])

    assert result.exit_code == 2
    assert "--task must not be empty" in result.output


@pytest.mark.parametrize("path", ["", " \t"])
def test_task_rejects_blank_file_path(validation_cli, path):
    result = validation_cli(["--task-file", path])

    assert result.exit_code == 2
    assert "--task-file path must not be empty" in result.output


def test_task_rejects_both_inputs_even_when_text_is_empty(validation_cli, tmp_path):
    path = tmp_path / "task.txt"
    path.write_text("from file", encoding="utf-8")

    result = validation_cli(["--task", "", "--task-file", str(path)])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


@pytest.mark.parametrize("source", ["text", "file"])
@pytest.mark.parametrize("existing_goal", ["old goal", None])
def test_task_rejects_goal_already_in_args(validation_cli, tmp_path, source, existing_goal):
    path = tmp_path / "task.txt"
    path.write_text("from file", encoding="utf-8")
    task_options = ["--task-file", str(path)] if source == "file" else ["--task", "new goal"]

    result = validation_cli([*task_options, "--args", json.dumps({"goal": existing_goal})])

    assert result.exit_code == 2
    assert "already contains 'goal'" in result.output


@pytest.mark.parametrize("kind", ["empty", "invalid-utf8", "missing", "directory", "fifo", "symlink", "oversized"])
def test_task_file_rejects_invalid_input(validation_cli, tmp_path, kind):
    path = tmp_path / "task.txt"
    if kind == "empty":
        path.write_text(" \t\n", encoding="utf-8")
    elif kind == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "real.txt"
        target.write_text("from symlink", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "oversized":
        path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

    result = validation_cli(["--task-file", str(path)])

    assert result.exit_code == 2
    expected = "--task-file is empty" if kind == "empty" else "Cannot read --task-file"
    assert expected in result.output


def test_workflow_list_prints_executable_next_step(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)

    result = CliRunner().invoke(workflow_cli.app, ["list", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert result.output.rstrip().endswith("Next run opencollab workflow run duo --help")


def test_workflow_run_help_explains_task_goal_and_file_limit():
    result = CliRunner().invoke(workflow_cli.app, ["run", "--help"])
    normalized = " ".join(unstyle(result.output).replace("│", " ").split())

    assert result.exit_code == 0
    assert "--task" in normalized and "--task-file" in normalized
    assert "Set workflow goal as plain text" in normalized
    assert "UTF-8 text file (max 4 MiB)" in normalized
