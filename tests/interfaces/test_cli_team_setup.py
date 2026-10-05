"""Editable team configuration uses the same data and loader as a team run."""

from __future__ import annotations

import shlex

import pytest
import yaml
from typer.testing import CliRunner

import opencollab.adapters.cli.main as cli_main
import opencollab.bootstrap.container as container
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap.team_config import default_team_config, load_team_config


@pytest.fixture(autouse=True)
def configuration_only(monkeypatch, tmp_path):
    """The actual CLI commands must work without initializing a run."""
    monkeypatch.chdir(tmp_path)
    for variable in (
        "OPENCOLLAB_TEAM_FILE",
        "OPENCOLLAB_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    def unexpected_runtime(*args, **kwargs):
        pytest.fail("A team configuration command initialized the model runtime")

    monkeypatch.setattr(cli_main, "resolve_config", unexpected_runtime)
    monkeypatch.setattr(cli_main, "_run", unexpected_runtime)
    monkeypatch.setattr(container, "LLMClient", unexpected_runtime)
    monkeypatch.setattr(Scheduler, "__init__", unexpected_runtime)


def _team_file(path, role):
    path.write_text(
        yaml.safe_dump(
            {
                "entry": role,
                "roles": {role: {"prompt": "Complete the task.", "tools": ["file_read"]}},
                "topology": {role: []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_init_default_team_round_trips_every_resolved_field(tmp_path):
    result = CliRunner().invoke(cli_main.app, ["team", "init"])

    assert result.exit_code == 0, result.output
    path = tmp_path / "team.yaml"
    exported = load_team_config(path=path)
    built_in = default_team_config()
    assert exported == built_in
    text = path.read_text(encoding="utf-8")
    assert text.count("prompt: |") == len(built_in.roles)
    assert "opencollab --team-config team.yaml --workspace ." in result.output
    assert "Edit the roles, prompts, tools and topology" in result.output


def test_init_uses_built_in_team_even_with_an_environment_team(tmp_path, monkeypatch):
    environment_team = _team_file(tmp_path / "environment.yaml", "custom")
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(environment_team))

    result = CliRunner().invoke(cli_main.app, ["team", "init"])

    assert result.exit_code == 0, result.output
    assert load_team_config(path=tmp_path / "team.yaml") == default_team_config()


def test_init_quotes_custom_path_and_prints_rich_markup_literally(tmp_path):
    name = "team [red] custom's $(touch marker) `x`.yaml"
    result = CliRunner().invoke(cli_main.app, ["team", "init", name])

    assert result.exit_code == 0, result.output
    assert (tmp_path / name).is_file()
    assert name in result.output
    command = next(line for line in result.output.splitlines() if line.startswith("opencollab "))
    assert shlex.split(command) == ["opencollab", "--team-config", name, "--workspace", "."]
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("contents", [b"existing user configuration\n", b""], ids=["populated", "empty"])
def test_init_preserves_existing_file(tmp_path, contents):
    path = tmp_path / "team.yaml"
    path.write_bytes(contents)

    result = CliRunner().invoke(cli_main.app, ["team", "init"])

    assert result.exit_code == 2, result.output
    assert "Team file already exists" in result.output
    assert "team.yaml" in result.output
    assert path.read_bytes() == contents


def test_init_reports_missing_destination_directory(tmp_path):
    result = CliRunner().invoke(cli_main.app, ["team", "init", "missing/team.yaml"])

    assert result.exit_code == 2, result.output
    assert "Destination directory does not exist" in result.output
    assert "missing" in result.output
    assert not (tmp_path / "missing").exists()


def test_init_preserves_existing_directory(tmp_path):
    path = tmp_path / "team.yaml"
    path.mkdir()

    result = CliRunner().invoke(cli_main.app, ["team", "init"])

    assert result.exit_code == 2, result.output
    assert "Team file already exists" in result.output
    assert path.is_dir()


def test_show_displays_built_in_roles_tools_entry_and_topology(tmp_path):
    result = CliRunner().invoke(cli_main.app, ["team", "show"])

    assert result.exit_code == 0, result.output
    assert "Configuration — built-in Self-Collaboration" in result.output
    assert "Entry role — analyst" in result.output
    assert "Configured roles — analyst, coder, tester" in result.output
    assert "active sessions as work is delegated" in " ".join(result.output.split())
    for name, role in default_team_config().roles.items():
        assert f"Declared tools for {name} — {', '.join(role.tools)}" in result.output
    assert "analyst → coder, tester" in result.output
    assert "coder → (no outgoing roles)" in result.output
    assert "tester → (no outgoing roles)" in result.output
    assert not (tmp_path / ".opencollab").exists()


@pytest.mark.parametrize("explicit", [False, True], ids=["environment", "explicit-priority"])
def test_show_uses_explicit_path_before_environment_team(tmp_path, monkeypatch, explicit):
    environment_team = _team_file(tmp_path / "environment.yaml", "environment")
    chosen_team = _team_file(tmp_path / "chosen.yaml", "chosen")
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(environment_team))
    arguments = ["--team-config", str(chosen_team)] if explicit else []
    expected_role = "chosen" if explicit else "environment"

    result = CliRunner().invoke(cli_main.app, ["team", "show", *arguments])

    assert result.exit_code == 0, result.output
    assert f"Entry role — {expected_role}" in result.output
    assert f"Configured roles — {expected_role}" in result.output
    assert f"Declared tools for {expected_role} — file_read" in result.output
    assert "analyst" not in result.output


def test_show_does_not_discover_conventional_team_files(tmp_path):
    _team_file(tmp_path / "team.yaml", "local")
    configs = tmp_path / "configs"
    configs.mkdir()
    _team_file(configs / "team.yaml", "conventional")

    result = CliRunner().invoke(cli_main.app, ["team", "show"])

    assert result.exit_code == 0, result.output
    assert "Entry role — analyst" in result.output
    assert "Configured roles — analyst, coder, tester" in result.output


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ("roles: [\n", "expected the node content"),
        ("roles: {}\nunknown_field: true\n", "Extra inputs are not permitted"),
        ("roles:\n  lead:\n    tools: []\n", "must set 'prompt' or 'prompt_file'"),
    ],
    ids=["yaml-syntax", "unknown-field", "missing-prompt"],
)
def test_show_reports_readable_configuration_errors(tmp_path, contents, reason):
    path = tmp_path / "broken.yaml"
    path.write_text(contents, encoding="utf-8")

    result = CliRunner().invoke(cli_main.app, ["team", "show", "--team-config", str(path)])

    assert result.exit_code == 2, result.output
    assert "Cannot load team configuration" in result.output
    assert reason in result.output
    assert "Traceback" not in result.output


def test_show_reports_missing_environment_team(tmp_path, monkeypatch):
    path = tmp_path / "missing.yaml"
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(path))

    result = CliRunner().invoke(cli_main.app, ["team", "show"])

    assert result.exit_code == 2, result.output
    assert "team config does not exist" in result.output
    assert str(path) in result.output


def test_show_prints_selected_path_literally(tmp_path):
    path = _team_file(tmp_path / "[red]team[bold].yaml", "custom")

    result = CliRunner().invoke(cli_main.app, ["team", "show", "--team-config", str(path)])

    assert result.exit_code == 0, result.output
    assert str(path) in result.output


@pytest.mark.parametrize("command", [[], ["init"], ["show"]], ids=["team", "init", "show"])
def test_team_help_works_without_credentials_or_creating_files(tmp_path, command):
    result = CliRunner().invoke(cli_main.app, ["team", *command, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert not (tmp_path / "team.yaml").exists()
    assert not (tmp_path / ".opencollab").exists()
