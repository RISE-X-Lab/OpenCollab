"""Unit tests for the YAML-backed team configuration loader."""

from __future__ import annotations

import pytest

from opencollab.bootstrap.team_config import (
    BASE_TOOL_NAMES,
    LEAD_TOOL_NAMES,
    RoleConfig,
    default_team_config,
    load_team_config,
)

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


def test_default_team_is_lead_only_with_allow_all(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_TEAM_FILE", raising=False)
    cfg = default_team_config()
    assert set(cfg.roles) == {"lead"}
    assert cfg.roles["lead"].tools == list(LEAD_TOOL_NAMES)
    assert cfg.topology.allow_all is True
    assert cfg.topology.allows("lead", "any-custom-role")


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
