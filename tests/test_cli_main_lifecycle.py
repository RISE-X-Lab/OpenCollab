from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from asyncio_test_support import assert_cancel_reason

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


class FakeConsole:
    def print(self, *args, **kwargs):
        return None


class FakeTUI:
    def __init__(self, *args, **kwargs):
        return None

    def print_welcome(self, **kwargs):
        return None

    def set_team_provider(self, provider):
        return None

    def reset(self):
        return None

    def start_live(self):
        return None

    def stop_live(self):
        return None

    def print_stats(self, *args):
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

        async def run(self, line):
            raise RuntimeError("scheduler failed")

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
        )

    assert cleanup_calls == 1
    assert tracer.closed is True


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

        async def run(self, line):
            run_started.set()
            await asyncio.Event().wait()

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

        async def run(self, line):
            raise RuntimeError("primary scheduler failure")

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
