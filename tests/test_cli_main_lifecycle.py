from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from asyncio_test_support import assert_cancel_reason
from click import unstyle
from typer.testing import CliRunner

import opencollab.adapters.cli.main as cli_main
import opencollab.adapters.tui as tui_mod
import opencollab.bootstrap as bootstrap


def test_repository_launcher_resolves_symlinked_checkout_to_physical_root(tmp_path):
    physical_parent = tmp_path / "physical"
    physical_root = physical_parent / "OpenCollab"
    scripts_dir = physical_root / "scripts"
    config_dir = physical_root / "configs"
    cli_dir = physical_root / ".venv" / "bin"
    scripts_dir.mkdir(parents=True)
    config_dir.mkdir()
    cli_dir.mkdir(parents=True)

    source_launcher = Path(__file__).resolve().parents[1] / "scripts" / "start_opencollab.sh"
    launcher = scripts_dir / "start_opencollab.sh"
    launcher.write_bytes(source_launcher.read_bytes())
    launcher.chmod(0o755)
    (config_dir / ".env").write_text("OPENCOLLAB_API_KEY=test-key\n", encoding="utf-8")
    fake_cli = cli_dir / "opencollab"
    fake_cli.write_text('#!/bin/sh\nprintf "arg=%s\\n" "$@"\n', encoding="utf-8")
    fake_cli.chmod(0o755)

    logical_parent = tmp_path / "logical"
    logical_parent.symlink_to(physical_parent, target_is_directory=True)
    logical_root = logical_parent / "OpenCollab"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'cd "$1" && ./scripts/start_opencollab.sh --probe',
            "launcher-test",
            str(logical_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"arg={physical_root}" in result.stdout
    assert f"arg={logical_root}" not in result.stdout


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_cli_prompt_file_rejects_unsafe_or_oversized_input(
    tmp_path,
    monkeypatch,
    kind,
):
    prompt = tmp_path / "prompt.txt"
    if kind == "fifo":
        os.mkfifo(prompt)
    elif kind == "symlink":
        real = tmp_path / "real.txt"
        real.write_text("secret", encoding="utf-8")
        prompt.symlink_to(real)
    else:
        prompt.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(cli_main, "MAX_CLI_PROMPT_FILE_BYTES", 64)

    with pytest.raises(typer.BadParameter, match="Cannot read --prompt-file"):
        cli_main._resolve_one_shot_prompt(None, str(prompt))


@pytest.mark.parametrize("prompt", ["", "   ", "\t\n"])
def test_cli_rejects_explicit_blank_prompt(prompt):
    with pytest.raises(typer.BadParameter, match="--prompt must not be empty"):
        cli_main._resolve_one_shot_prompt(prompt, None)


def test_cli_rejects_empty_prompt_file_path():
    with pytest.raises(typer.BadParameter, match="--prompt-file path must not be empty"):
        cli_main._resolve_one_shot_prompt(None, "")


def test_cli_rejects_both_prompt_inputs_even_when_prompt_is_empty(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="mutually exclusive"):
        cli_main._resolve_one_shot_prompt("", str(prompt_file))


def test_cli_preserves_nonblank_unicode_prompt():
    assert cli_main._resolve_one_shot_prompt(" Grüß dich 🌍 ", None) == " Grüß dich 🌍 "


class FakeConsole:
    # Chrome lines are truncated to the console width, as a real Console has.
    width = 80

    def print(self, *args, **kwargs):
        return None


class FakeTUI:
    def __init__(self, *args, **kwargs):
        self.selected_aid = 0

    def print_welcome(self, **kwargs):
        return None

    def set_team_provider(self, provider):
        return None

    def select_agent(self, aid):
        self.selected_aid = aid
        return aid

    def record_user_message(self, aid, content):
        return None

    def reset(self):
        return None

    def set_redraw(self, redraw):
        return None

    def set_queued_turns(self, count):
        return None

    def hud_ansi(self, width=None):
        return None

    def start_turn(self, aid):
        return None

    def settle_turn(self, **kwargs):
        return None

    def drained_partial_answer(self, aid):
        return False

    def print_stats(self, *args):
        return None

    def print_turn_divider(self):
        return None


class FakeTracer:
    path = "/tmp/trajectory.jsonl"

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def install_cli_fakes(monkeypatch, scheduler, tracer):
    ctx = SimpleNamespace(tracer=tracer)
    monkeypatch.setattr(cli_main, "console", FakeConsole())
    monkeypatch.setattr(tui_mod, "TUI", FakeTUI)
    monkeypatch.setattr(tui_mod, "TuiAskUserPolicy", lambda **kwargs: object())
    monkeypatch.setattr(tui_mod, "TuiEventSink", lambda tui: object())
    monkeypatch.setattr(bootstrap, "build_runtime_context", lambda *a, **k: ctx)
    monkeypatch.setattr(bootstrap, "build_scheduler", lambda *a, **k: scheduler)
    return ctx


def config():
    return {
        "filter_messages": False,
        "model": "model",
        "provider": "provider",
        "budget": 100,
    }


def test_cli_forwards_explicit_team_config(monkeypatch, tmp_path):
    captured = {}
    team_config = tmp_path / "custom-team.yaml"
    resolved = {
        **config(),
        "api_key": "test-key",  # pragma: allowlist secret
        "base_url": None,
    }

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "resolve_config", lambda *args: resolved)
    monkeypatch.setattr(cli_main, "missing_api_key_for", lambda *args: False)
    monkeypatch.setattr(cli_main, "_run", fake_run)
    monkeypatch.setattr(
        cli_main,
        "run_with_bounded_shutdown",
        lambda coroutine: asyncio.run(coroutine),
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "--team-config",
            str(team_config),
            "--prompt",
            "do work",
            "--hold",
            "--allow-local-child-tests",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["team_config_path"] == str(team_config)
    assert captured["hold_after_run"] is True
    assert captured["allow_unisolated_child_tests"] is True


def test_cli_hold_requires_one_shot_prompt():
    result = CliRunner().invoke(cli_main.app, ["--hold"], color=True)

    assert result.exit_code == 2
    assert "--hold requires --prompt or --prompt-file" in unstyle(result.output)


@pytest.mark.asyncio
async def test_cli_run_passes_team_config_path_to_scheduler(monkeypatch, tmp_path):
    captured = {}
    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, object(), tracer)
    team_config = tmp_path / "custom-team.yaml"

    def capture_build_scheduler(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(bootstrap, "build_scheduler", capture_build_scheduler)

    with pytest.raises(RuntimeError, match="stop after capture"):
        await cli_main._run(
            str(tmp_path),
            config(),
            None,
            True,
            True,
            False,
            team_config_path=str(team_config),
            one_shot_prompt="do work",
            allow_unisolated_child_tests=True,
        )

    assert captured["team_config_path"] == str(team_config)
    assert captured["allow_unisolated_child_tests"] is True
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_one_shot_failure_still_cleans_scheduler_and_closes_tracer(
    monkeypatch,
    tmp_path,
):
    cleanup_calls = 0

    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            assert aid == 0
            raise RuntimeError("scheduler failed")

        def agent_step_count(self, aid):
            return 0

        async def cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)

    with pytest.raises(RuntimeError, match="scheduler failed"):
        await cli_main._run(
            str(tmp_path),
            config(),
            None,
            True,
            True,
            False,
            one_shot_prompt="do work",
            hold_after_run=True,
        )

    assert cleanup_calls == 1
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_successful_one_shot_settles_then_cleans_up(monkeypatch, tmp_path):
    events: list[str] = []

    class Scheduler:
        used_tokens = 7
        lead_session = SimpleNamespace(auto_save_path=None, step_count=1)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            assert (aid, line) == (0, "do work")
            assert isinstance(cancel_event, asyncio.Event)
            events.append("run")

        def agent_step_count(self, aid):
            assert aid == 0
            return 1

        async def cleanup(self):
            events.append("cleanup")

    class LifecycleTUI(FakeTUI):
        def settle_turn(self, **kwargs):
            events.append("settle")

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)
    monkeypatch.setattr(tui_mod, "TUI", LifecycleTUI)

    await cli_main._run(
        str(tmp_path),
        config(),
        None,
        True,
        True,
        False,
        one_shot_prompt="do work",
    )

    assert events == ["run", "settle", "cleanup"]
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_hold_without_a_screen_still_ends_when_the_turn_drains(
    monkeypatch,
    tmp_path,
):
    """``--hold`` keeps the prompt up, so a run with no prompt has none to keep.

    Redirected output has no bottom region to hold open; waiting for a user to
    leave one would hang a run that nobody is watching.
    """
    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            return None

        def agent_step_count(self, aid):
            return 0

        async def cleanup(self):
            return None

    install_cli_fakes(monkeypatch, Scheduler(), FakeTracer())
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: False, raising=False)

    await asyncio.wait_for(
        cli_main._run(
            str(tmp_path),
            config(),
            None,
            True,
            True,
            False,
            one_shot_prompt="do work",
            hold_after_run=True,
        ),
        timeout=5,
    )


@pytest.mark.asyncio
async def test_one_shot_prints_the_answer_even_if_focus_wandered_to_a_teammate(
    monkeypatch,
    tmp_path,
):
    """A one-shot run has no "later" in which to Tab back to the lead.

    Only the focused agent's blocks reach scrollback, so a run that ends while
    the user is watching a teammate would exit with the answer it was asked for
    settled in memory and never printed.

    Exactly once, too: the flush is a tail drain, not a focus switch. A focus
    switch reprints an agent in full, which would repeat every row the lead had
    already put on screen before the user wandered off.
    """
    from io import StringIO

    from rich.console import Console

    from opencollab.adapters.tui import TUI
    from opencollab.domain.events import SessionRuntimeEvent

    console = Console(file=StringIO(), width=100, color_system=None)
    holder: dict[str, TUI] = {}

    class OneShotTUI(TUI):
        def __init__(self, *args, **kwargs):
            super().__init__(console)
            holder["tui"] = self

    class Scheduler:
        used_tokens = 7
        lead_session = SimpleNamespace(auto_save_path=None, step_count=1)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            tui = holder["tui"]
            # Settled while the lead still holds focus: already in scrollback.
            tui.event_handler(
                SessionRuntimeEvent("text_delta", {"content": "an early lead line", "aid": 0})
            )
            tui.event_handler(
                SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "pwd"}, "aid": 0})
            )
            tui.event_handler(
                SessionRuntimeEvent("text_delta", {"content": "the final answer", "aid": 0})
            )
            tui.event_handler(
                SessionRuntimeEvent("text_delta", {"content": "teammate notes", "aid": 1})
            )
            # Settles the teammate's text, so focusing it has something to show.
            tui.event_handler(
                SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "ls"}, "aid": 1})
            )
            # The user Tabs to the teammate to read its trajectory, and the run
            # ends there.
            tui.select_agent(1)

        def agent_step_count(self, aid):
            return 1

        async def cleanup(self):
            return None

    install_cli_fakes(monkeypatch, Scheduler(), FakeTracer())
    monkeypatch.setattr(tui_mod, "TUI", OneShotTUI)

    await cli_main._run(
        str(tmp_path),
        config(),
        None,
        True,
        True,
        False,
        one_shot_prompt="do work",
    )

    scrollback = console.file.getvalue()
    assert "teammate notes" in scrollback
    assert scrollback.count("the final answer") == 1
    assert scrollback.count("an early lead line") == 1


@pytest.mark.asyncio
async def test_cli_warns_when_event_log_persistence_is_degraded(
    monkeypatch,
    tmp_path,
    capsys,
):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv(
        "OPENCOLLAB_EVENTS_FILE",
        str(blocker / "events.jsonl"),
    )

    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            assert (aid, line) == (0, "do work")

        def agent_step_count(self, aid):
            return 0

        async def cleanup(self):
            return None

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)

    await cli_main._run(
        str(tmp_path),
        config(),
        None,
        True,
        True,
        False,
        one_shot_prompt="do work",
    )

    warning = capsys.readouterr().err
    assert "event log persistence degraded" in warning
    assert "dropped events: 0" in warning
    assert str(blocker / "events.jsonl") in warning


@pytest.mark.asyncio
async def test_cli_double_cancel_waits_for_cleanup_then_closes_tracer(
    monkeypatch,
    tmp_path,
):
    run_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            assert aid == 0
            run_started.set()
            await asyncio.Event().wait()

        def agent_step_count(self, aid):
            return 0

        async def cleanup(self):
            cleanup_started.set()
            await release_cleanup.wait()

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)
    execution = asyncio.create_task(
        cli_main._run(
            str(tmp_path),
            config(),
            None,
            True,
            True,
            False,
            one_shot_prompt="do work",
        )
    )

    await run_started.wait()
    execution.cancel("first")
    await cleanup_started.wait()
    execution.cancel("second")
    await asyncio.sleep(0)
    assert execution.done() is False
    assert tracer.closed is False

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await execution

    assert_cancel_reason(cancelled.value, "first")
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_team_provider_failure_still_cleans_scheduler_and_tracer(
    monkeypatch,
    tmp_path,
):
    cleanup_calls = 0

    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1

    def fail_team_provider(self, provider):
        raise RuntimeError("team provider failed")

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)
    monkeypatch.setattr(FakeTUI, "set_team_provider", fail_team_provider)

    with pytest.raises(RuntimeError, match="team provider failed"):
        await cli_main._run(
            str(tmp_path), config(), None, True, True, False,
            one_shot_prompt="do work",
        )

    assert cleanup_calls == 1
    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_scheduler_build_failure_still_closes_tracer(monkeypatch, tmp_path):
    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, object(), tracer)
    monkeypatch.setattr(
        bootstrap,
        "build_scheduler",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("scheduler build failed")
        ),
    )

    with pytest.raises(RuntimeError, match="scheduler build failed"):
        await cli_main._run(
            str(tmp_path), config(), None, True, True, False,
            one_shot_prompt="do work",
        )

    assert tracer.closed is True


@pytest.mark.asyncio
async def test_cli_trajectory_print_failure_does_not_mask_primary_error(
    monkeypatch,
    tmp_path,
):
    class Scheduler:
        used_tokens = 0
        lead_session = SimpleNamespace(auto_save_path=None, step_count=0)

        def team_roster(self):
            return []

        async def run_turn(self, aid, line, *, cancel_event=None):
            assert aid == 0
            raise RuntimeError("primary scheduler failure")

        def agent_step_count(self, aid):
            return 0

        async def cleanup(self):
            return None

    class PrintFailureConsole(FakeConsole):
        def print(self, *args, **kwargs):
            if args and "trajectory" in str(args[0]):
                raise OSError("console failed")

    tracer = FakeTracer()
    install_cli_fakes(monkeypatch, Scheduler(), tracer)
    monkeypatch.setattr(cli_main, "console", PrintFailureConsole())

    with pytest.raises(RuntimeError, match="primary scheduler failure") as raised:
        await cli_main._run(
            str(tmp_path), config(), None, True, True, False,
            one_shot_prompt="do work",
        )

    assert any("console failed" in note for note in raised.value.__notes__)
