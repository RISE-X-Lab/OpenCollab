"""Entry-point guidance and local help for interactive users."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from click import unstyle
from rich.console import Console
from typer.testing import CliRunner

from opencollab.adapters.cli import main as cli_main
from opencollab.adapters.tui import TUI


def test_root_help_explains_the_three_entry_points_without_loading_config(monkeypatch):
    def unexpected_config(*args, **kwargs):
        pytest.fail("Help must work before configuring a model")

    monkeypatch.setattr(cli_main, "resolve_config", unexpected_config)
    result = CliRunner().invoke(cli_main.app, ["--help"], terminal_width=120)
    output = " ".join(unstyle(result.stdout).replace("│", " ").split())

    assert result.exit_code == 0
    assert "Run without a subcommand to chat" in output
    assert "opencollab team init team.yaml" in output
    assert "opencollab team show --team-config team.yaml" in output
    assert "opencollab workflow run duo --help" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_pending", [False, True])
async def test_repl_help_is_local_and_preserves_question_priority(monkeypatch, answer_pending):
    output = StringIO()
    monkeypatch.setattr(cli_main, "console", Console(file=output, width=120, color_system=None))
    lines = iter(["/help", "continue the task", "/exit"])
    answered = []
    submitted = []

    async def read():
        return next(lines)

    def deliver(line):
        if answer_pending and not answered:
            answered.append(line)
            return True
        return False

    prompt = SimpleNamespace(read=read, deliver=deliver)
    queue = SimpleNamespace(submit=lambda line, aid: submitted.append((line, aid)))
    tui = SimpleNamespace(selected_aid=2)

    await cli_main._read_loop(prompt, queue, tui, object())

    assert submitted == [("continue the task", 2)]
    if answer_pending:
        assert answered == ["/help"]
        assert output.getvalue() == ""
    else:
        assert answered == []
        assert "Interactive team help" in output.getvalue()
        assert "Tab / Shift+Tab" in output.getvalue()
        assert "lead agent" in output.getvalue()
        assert "--session PATH" in output.getvalue()


def test_interactive_welcome_advertises_local_help():
    output = StringIO()
    tui = TUI(Console(file=output, width=120, color_system=None))
    tui.print_welcome(interactive=True)
    assert "/help for controls" in output.getvalue()

    output.seek(0)
    output.truncate()
    tui.print_welcome(interactive=False)
    assert "/help" not in output.getvalue()
