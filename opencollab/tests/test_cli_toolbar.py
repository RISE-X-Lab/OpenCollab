from __future__ import annotations

from prompt_toolkit.formatted_text import to_formatted_text

from opencollab.adapters.cli.main import _PROMPT_STYLE, _format_team_toolbar


def _plain_and_fragments(toolbar):
    fragments = list(to_formatted_text(toolbar))
    plain = "".join(text for _, text in fragments)
    return plain, fragments


def test_team_toolbar_displays_scheduled_lead_as_idle_in_green():
    toolbar = _format_team_toolbar([{"aid": 0, "phase": "scheduled", "busy": False}])
    plain, fragments = _plain_and_fragments(toolbar)

    assert plain == "Team: Lead(idle)"
    assert ("fg:ansigreen", "idle") in fragments
    for style, text in fragments:
        if text.strip():
            assert style
            assert "white" not in style


def test_team_toolbar_escapes_role_text():
    toolbar = _format_team_toolbar([{"aid": 2, "role": "dev<ops>", "phase": "running"}])
    plain, fragments = _plain_and_fragments(toolbar)

    assert plain == "Team: A2 dev<ops>(running)"
    assert ("fg:ansiyellow", "running") in fragments


def test_team_toolbar_displays_completed_non_busy_agent_as_idle():
    toolbar = _format_team_toolbar(
        [{"aid": 1, "role": "coder", "phase": "done", "busy": False}]
    )
    plain, _ = _plain_and_fragments(toolbar)

    assert plain == "Team: A1 coder(idle)"


def test_prompt_toolbar_style_does_not_use_reverse_or_solid_background():
    for selector in ("class:bottom-toolbar", "class:bottom-toolbar.text"):
        attrs = _PROMPT_STYLE.get_attrs_for_style_str(selector)
        assert attrs.bgcolor == "default"
        assert attrs.reverse is False
        assert attrs.color != "ansiwhite"
