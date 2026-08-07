"""Unit tests for the YAML-backed team configuration loader."""

from __future__ import annotations

import os

import pytest

from opencollab.bootstrap import team_config as team_config_mod
from opencollab.bootstrap.team_config import (
    BASE_TOOL_NAMES,
    DEFAULT_LEAD_PROMPT,
    DEFAULT_ROLE_PROMPT,
    LEAD_TOOL_NAMES,
    RoleConfig,
    TeamConfig,
    default_team_config,
    load_team_config,
    resolve_team_file,
)
from opencollab.domain.team import Topology

TEAM_YAML = """\
roles:
  lead:
    model: gpt-4o-mini
    tools: [bash, spawn_agent, message_agent]
    prompt: |
      Lead prompt.
  coder:
    tools: [bash, file_read, file_write, grep]
    prompt_file: prompts/coder.md
topology:
  lead: [coder]
  coder: [reviewer]
"""


def _write_team(tmp_path, monkeypatch, yaml_text=TEAM_YAML, coder_prompt="Coder prompt."):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(yaml_text)
    prompts = configs / "prompts"
    prompts.mkdir()
    (prompts / "coder.md").write_text(coder_prompt)
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))


def test_default_prompts_load_from_packaged_files():
    # The built-in defaults are data files under bootstrap/prompts/; guard that
    # they load (non-empty, correct identity) so a packaging regression fails here
    # rather than silently shipping an empty system prompt.
    assert DEFAULT_LEAD_PROMPT.startswith("You are OpenCollab, agent 0")
    assert "spawn_with_review" in DEFAULT_LEAD_PROMPT
    assert DEFAULT_ROLE_PROMPT.startswith("You are an OpenCollab specialist agent.")


def test_lead_prompt_has_anti_thrash_recon_strategy():
    # The per-read distill rule lives in the file_read tool description (universal
    # across all workflows/teams). The lead prompt keeps only the lead-specific
    # strategy: stop reading once notes cover the task, and delegate sprawling
    # recon. Regression for the 90-read/0-write stall (session 2026-06-21T20-28-41).
    assert "thrash" in DEFAULT_LEAD_PROMPT
    assert "STOP reading" in DEFAULT_LEAD_PROMPT
    assert "spawn_agent" in DEFAULT_LEAD_PROMPT  # delegate when recon sprawls


def test_default_team_is_lead_only_with_allow_all(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_TEAM_FILE", raising=False)
    cfg = default_team_config()
    assert set(cfg.roles) == {"lead"}
    assert cfg.roles["lead"].tools == list(LEAD_TOOL_NAMES)
    assert cfg.topology.allow_all is True
    assert cfg.topology.allows("lead", "any-custom-role")


def test_load_team_ignores_conventional_workspace_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_TEAM_FILE", raising=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(
        "roles:\n  custom:\n    prompt: Custom team.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = load_team_config(str(tmp_path))

    assert set(cfg.roles) == {"lead"}
    assert cfg.entry == "lead"
    assert resolve_team_file(str(tmp_path)) is None


def test_load_team_accepts_explicit_path_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_TEAM_FILE", raising=False)
    config = tmp_path / "custom-team.yaml"
    config.write_text(
        "roles:\n  captain:\n    prompt: Lead explicitly.\n",
        encoding="utf-8",
    )

    cfg = load_team_config(str(tmp_path), path=config)

    assert set(cfg.roles) == {"captain"}
    assert cfg.entry == "captain"


def test_explicit_path_takes_precedence_over_environment(tmp_path, monkeypatch):
    environment_config = tmp_path / "environment-team.yaml"
    environment_config.write_text(
        "roles:\n  environment:\n    prompt: Environment team.\n",
        encoding="utf-8",
    )
    argument_config = tmp_path / "argument-team.yaml"
    argument_config.write_text(
        "roles:\n  argument:\n    prompt: Argument team.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(environment_config))

    cfg = load_team_config(str(tmp_path), path=argument_config)

    assert set(cfg.roles) == {"argument"}
    assert cfg.entry == "argument"


def test_load_team_rejects_missing_explicit_environment_path(tmp_path, monkeypatch):
    missing = tmp_path / "missing-team.yaml"
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(missing))

    with pytest.raises(ValueError, match="team config does not exist"):
        load_team_config(str(tmp_path))


def test_load_team_roundtrip_roles_and_topology(tmp_path, monkeypatch):
    _write_team(tmp_path, monkeypatch)
    cfg = load_team_config(str(tmp_path))

    assert set(cfg.roles) == {"lead", "coder"}
    assert cfg.roles["lead"].model == "gpt-4o-mini"
    assert cfg.roles["lead"].tools == ["bash", "spawn_agent", "message_agent"]
    assert cfg.topology.allow_all is False
    assert cfg.topology.allows("lead", "coder")
    assert cfg.topology.allows("coder", "reviewer")
    assert not cfg.topology.allows("lead", "reviewer")


def test_role_temperature_defaults_to_none_when_unset(tmp_path, monkeypatch):
    # TEAM_YAML declares no temperature for either role → both stay None so the
    # ContextBuilder falls back to the global OpenCollabConfig default.
    _write_team(tmp_path, monkeypatch)
    cfg = load_team_config(str(tmp_path))
    assert cfg.roles["lead"].temperature is None
    assert cfg.roles["coder"].temperature is None


def test_role_temperature_override_is_parsed(tmp_path, monkeypatch):
    yaml_text = """\
roles:
  lead:
    temperature: 0.0
    tools: [bash, spawn_agent]
    prompt: |
      Lead prompt.
  coder:
    temperature: 0.9
    tools: [bash, file_write]
    prompt: |
      Coder prompt.
topology:
  lead: [coder]
"""
    _write_team(tmp_path, monkeypatch, yaml_text=yaml_text)
    cfg = load_team_config(str(tmp_path))
    assert cfg.roles["lead"].temperature == 0.0
    assert cfg.roles["coder"].temperature == 0.9


def test_prompt_file_is_resolved_relative_to_team_file(tmp_path, monkeypatch):
    _write_team(tmp_path, monkeypatch, coder_prompt="Resolved coder body.")
    cfg = load_team_config(str(tmp_path))
    assert cfg.roles["coder"].prompt == "Resolved coder body."


def test_unknown_role_falls_back_to_generic_spec(tmp_path, monkeypatch):
    _write_team(tmp_path, monkeypatch)
    cfg = load_team_config(str(tmp_path))
    fallback = cfg.role_for("totally-new-role")
    assert isinstance(fallback, RoleConfig)
    # Ad-hoc roles get the registry-derived worker bundle: no coordination tools
    # (they must not fan out further) and no skill dispatch.
    assert fallback.tools == list(BASE_TOOL_NAMES)
    assert "spawn_agent" not in fallback.tools
    assert "use_skill" not in fallback.tools


def test_default_team_entry_is_lead(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_TEAM_FILE", raising=False)
    assert default_team_config().entry == "lead"


def test_entry_prefers_lead_role_when_unset(tmp_path, monkeypatch):
    # TEAM_YAML declares lead + coder with no explicit entry → lead wins.
    _write_team(tmp_path, monkeypatch)
    assert load_team_config(str(tmp_path)).entry == "lead"


def test_entry_falls_back_to_first_role_without_lead(tmp_path, monkeypatch):
    yaml_text = """\
roles:
  analyst:
    tools: [bash, spawn_agent]
    prompt: |
      Analyst prompt.
  coder:
    tools: [bash, file_write]
    prompt: |
      Coder prompt.
topology:
  analyst: [coder]
"""
    _write_team(tmp_path, monkeypatch, yaml_text=yaml_text)
    assert load_team_config(str(tmp_path)).entry == "analyst"


def test_explicit_entry_is_respected(tmp_path, monkeypatch):
    yaml_text = """\
entry: coder
roles:
  lead:
    tools: [bash, spawn_agent]
    prompt: |
      Lead prompt.
  coder:
    tools: [bash, file_write]
    prompt: |
      Coder prompt.
topology:
  lead: [coder]
"""
    _write_team(tmp_path, monkeypatch, yaml_text=yaml_text)
    assert load_team_config(str(tmp_path)).entry == "coder"


def test_explicit_entry_naming_undeclared_role_raises(tmp_path, monkeypatch):
    yaml_text = """\
entry: nope
roles:
  lead:
    tools: [bash]
    prompt: |
      Lead prompt.
"""
    _write_team(tmp_path, monkeypatch, yaml_text=yaml_text)
    with pytest.raises(ValueError, match="entry role 'nope'"):
        load_team_config(str(tmp_path))


def test_missing_prompt_and_prompt_file_raises(tmp_path, monkeypatch):
    bad = """\
roles:
  coder:
    tools: [bash]
"""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(bad)
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))
    with pytest.raises(ValueError, match="prompt"):
        load_team_config(str(tmp_path))


@pytest.mark.parametrize(
    "yaml_text",
    [
        "rolse:\n  lead:\n    prompt: typo\n",
        "roles:\n  lead:\n    prompt: ok\n    modle: typo\n",
    ],
)
def test_team_config_rejects_unknown_keys(tmp_path, monkeypatch, yaml_text):
    config = tmp_path / "team.yaml"
    config.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(Exception, match="rolse|modle"):
        load_team_config(str(tmp_path), path=config)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_team_config_rejects_unsafe_or_oversized_input(tmp_path, monkeypatch, kind):
    config = tmp_path / "team.yaml"
    if kind == "fifo":
        os.mkfifo(config)
    elif kind == "symlink":
        real = tmp_path / "real.yaml"
        real.write_text("roles: {}\n", encoding="utf-8")
        config.symlink_to(real)
    else:
        config.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(team_config_mod, "MAX_TEAM_CONFIG_BYTES", 64)
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(config))

    with pytest.raises(ValueError, match="team config"):
        load_team_config(str(tmp_path))


def test_role_prompt_file_cannot_escape_team_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(
        "roles:\n  lead:\n    tools: [bash]\n"
        "    prompt_file: ../outside.md\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))

    with pytest.raises(ValueError, match="escapes team directory"):
        load_team_config(str(tmp_path))


@pytest.mark.parametrize("kind", ["symlink", "oversized"])
def test_role_prompt_file_rejects_unsafe_or_oversized_input(
    tmp_path,
    monkeypatch,
    kind,
):
    configs = tmp_path / "configs"
    configs.mkdir()
    prompt = configs / "role.md"
    if kind == "symlink":
        real = tmp_path / "real.md"
        real.write_text("outside", encoding="utf-8")
        prompt.symlink_to(real)
    else:
        prompt.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(team_config_mod, "MAX_ROLE_PROMPT_BYTES", 64)
    (configs / "team.yaml").write_text(
        "roles:\n  lead:\n    tools: [bash]\n    prompt_file: role.md\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))

    with pytest.raises(ValueError, match="cannot be read safely"):
        load_team_config(str(tmp_path))


@pytest.mark.parametrize(
    "yaml_text",
    [
        "roles:\n  '../outside':\n    prompt: unsafe\n",
        "roles:\n  'line\\nbreak':\n    prompt: unsafe\n",
        "roles:\n  lead:\n    prompt: ok\nentry: '../outside'\n",
        "roles:\n  lead:\n    prompt: ok\ntopology:\n  lead: ['../outside']\n",
    ],
)
def test_team_config_rejects_unsafe_role_identity(tmp_path, monkeypatch, yaml_text):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))

    with pytest.raises(ValueError, match="role"):
        load_team_config(str(tmp_path))


def test_team_config_rejects_casefold_role_collision(tmp_path, monkeypatch):
    yaml_text = (
        "roles:\n"
        "  Coder:\n    prompt: first\n"
        "  coder:\n    prompt: second\n"
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))

    with pytest.raises(ValueError, match="collide"):
        load_team_config(str(tmp_path))


def test_team_config_rejects_unicode_normalization_role_collision(
    tmp_path,
    monkeypatch,
):
    composed = "Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    decomposed = "Cafe\N{COMBINING ACUTE ACCENT}"
    yaml_text = (
        "roles:\n"
        f"  {composed!r}:\n    prompt: first\n"
        f"  {decomposed!r}:\n    prompt: second\n"
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))

    with pytest.raises(ValueError, match="collide"):
        load_team_config(str(tmp_path))


def test_programmatic_team_config_rejects_casefold_role_collision():
    role = RoleConfig(prompt="role", tools=[])

    with pytest.raises(ValueError, match="collide"):
        TeamConfig(
            roles={"Coder": role, "coder": role},
            topology=Topology(allow_all=True),
            entry="Coder",
        )


def test_programmatic_team_config_rejects_casefold_topology_source_collision():
    lead = RoleConfig(prompt="lead", tools=[])

    with pytest.raises(ValueError, match="topology source identities collide"):
        TeamConfig(
            roles={"lead": lead},
            topology=Topology(
                edges={
                    "Lead": frozenset({"coder"}),
                    "lead": frozenset({"reviewer"}),
                }
            ),
            entry="lead",
        )


def test_programmatic_team_config_uses_one_casefold_identity():
    coder = RoleConfig(prompt="coder prompt", tools=[])
    reviewer = RoleConfig(prompt="reviewer prompt", tools=[])
    team = TeamConfig(
        roles={"Coder": coder, "Reviewer": reviewer},
        topology=Topology(edges={"coder": frozenset({"reviewer"})}),
        entry="CODER",
    )

    assert team.entry == "Coder"
    assert team.role_for("coder") is coder
    assert team.role_for("CODER") is coder
    assert team.topology.allows("CODER", "REVIEWER") is True
