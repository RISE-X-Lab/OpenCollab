from __future__ import annotations

import pytest
from opencollab.adapters.cli.main import _PROMPT_STYLE, _dispatch_repl_command
from opencollab.adapters.cli.toolbar import format_team_toolbar
from prompt_toolkit.formatted_text import to_formatted_text


class _FakeLead:
    def __init__(self):
        self.saved_to: str | None = None

    def save(self, path: str) -> None:
        self.saved_to = path


def _plain_and_fragments(toolbar):
    fragments = list(to_formatted_text(toolbar))
    plain = "".join(text for _, text in fragments)
    return plain, fragments


def test_team_toolbar_displays_idle_lead_in_green():
    toolbar = format_team_toolbar([{"aid": 0, "phase": "idle", "busy": False}])
    plain, fragments = _plain_and_fragments(toolbar)

    assert plain == "Team: Lead(idle)"
    assert ("fg:ansigreen", "idle") in fragments
    for style, text in fragments:
        if text.strip():
            assert style
            assert "white" not in style


def test_team_toolbar_escapes_role_text():
    toolbar = format_team_toolbar([{"aid": 2, "role": "dev<ops>", "phase": "running"}])
    plain, fragments = _plain_and_fragments(toolbar)

    assert plain == "Team: A2 dev<ops>(running)"
    assert ("fg:ansiyellow", "running") in fragments


def test_team_toolbar_displays_completed_non_busy_agent_as_idle():
    toolbar = format_team_toolbar(
        [{"aid": 1, "role": "coder", "phase": "done", "busy": False}]
    )
    plain, _ = _plain_and_fragments(toolbar)

    assert plain == "Team: A1 coder(idle)"


def test_team_toolbar_displays_busy_agent_as_running():
    toolbar = format_team_toolbar(
        [{"aid": 1, "role": "coder", "phase": "executing_tools", "busy": True}]
    )
    plain, fragments = _plain_and_fragments(toolbar)

    assert plain == "Team: A1 coder(running)"
    assert ("fg:ansiyellow", "running") in fragments


@pytest.mark.parametrize("command", ["exit", "quit", "/exit", "/quit", "QUIT"])
def test_repl_exit_commands_break_the_loop(command):
    assert _dispatch_repl_command(command, _FakeLead()) is False


def test_repl_save_command_saves_lead_and_continues():
    lead = _FakeLead()
    assert _dispatch_repl_command("/save", lead) is True
    assert lead.saved_to is not None and lead.saved_to.startswith("session-")
    assert lead.saved_to.endswith(".jsonl")


def test_repl_non_command_signals_passthrough():
    with pytest.raises(KeyError):
        _dispatch_repl_command("write some code", _FakeLead())


def test_prompt_toolbar_style_does_not_use_reverse_or_solid_background():
    for selector in ("class:bottom-toolbar", "class:bottom-toolbar.text"):
        attrs = _PROMPT_STYLE.get_attrs_for_style_str(selector)
        assert attrs.bgcolor == "default"
        assert attrs.reverse is False
        assert attrs.color != "ansiwhite"
