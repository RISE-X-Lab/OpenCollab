"""Tests for the minimal (phase-1, observe-only) hook system.

Covers the four layers the feature touches: domain matching policy, the
application event→hook subscriber, the adapter shell runner, the config parser,
and the bootstrap wiring (including the eval-gating switch).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.adapters import hooks as hooks_adapter
from opencollab.adapters.hooks import ShellHookRunner
from opencollab.application.hooks import HookEventSubscriber
from opencollab.bootstrap import build_runtime_context, build_scheduler
from opencollab.bootstrap.team_config import load_team_config
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent
from opencollab.domain.hooks import HookOutcome, HookSpec, match_hooks

# ---------------------------------------------------------------------------
# domain: match_hooks
# ---------------------------------------------------------------------------


def _spec(event="PostToolUse", matcher=None, command="true"):
    return HookSpec(event=event, action_type="command", command=command, matcher=matcher)


def test_match_hooks_no_matcher_matches_with_or_without_tool():
    specs = (_spec(matcher=None),)
    assert match_hooks(specs, "PostToolUse", "file_write") == list(specs)
    assert match_hooks(specs, "PostToolUse", None) == list(specs)


def test_match_hooks_glob_hit_and_miss():
    specs = (_spec(matcher="file_*"),)
    assert match_hooks(specs, "PostToolUse", "file_write") == list(specs)
    assert match_hooks(specs, "PostToolUse", "bash") == []


def test_match_hooks_filters_by_event_name():
    specs = (_spec(event="PreToolUse"),)
    assert match_hooks(specs, "PostToolUse", "bash") == []


def test_match_hooks_with_matcher_never_fires_without_tool():
    specs = (_spec(matcher="file_*"),)
    assert match_hooks(specs, "PostToolUse", None) == []


# ---------------------------------------------------------------------------
# application: HookEventSubscriber mapping
# ---------------------------------------------------------------------------


class _RecordingRunner:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def fire(self, event_name, payload):
        self.calls.append((event_name, payload))
        return HookOutcome()


def _emit(event):
    runner = _RecordingRunner()
    asyncio.run(HookEventSubscriber(runner).emit(event))
    return runner.calls


def test_subscriber_maps_tool_events():
    calls = _emit(SessionRuntimeEvent(type="tool_start", data={"tool": "bash", "aid": 1}))
    assert calls == [("PreToolUse", {"hook_event_name": "PreToolUse", "tool": "bash", "aid": 1})]

    calls = _emit(SessionRuntimeEvent(type="tool_end", data={"tool": "grep", "aid": 2}))
    assert calls[0][0] == "PostToolUse"
    assert calls[0][1]["tool"] == "grep"


def test_subscriber_maps_session_lifecycle():
    assert _emit(SchedulerEvent(type="agent_spawned", data={"aid": 3}))[0][0] == "SessionStart"
    assert _emit(SessionRuntimeEvent(type="error", data={"reason": "boom"}))[0][0] == "Notification"


def test_subscriber_splits_completed_by_parent():
    # Lead (parent_aid is None) finishing == the team Stop.
    assert _emit(SchedulerEvent(type="agent_completed", data={"aid": 0, "parent_aid": None}))[0][0] == "Stop"
    # A child finishing == SubagentStop.
    assert _emit(SchedulerEvent(type="agent_completed", data={"aid": 5, "parent_aid": 0}))[0][0] == "SubagentStop"


def test_subscriber_ignores_unmapped_events():
    assert _emit(SessionRuntimeEvent(type="step_end", data={"step": 1})) == []
    assert _emit(SessionRuntimeEvent(type="text_delta", data={"content": "x"})) == []


# ---------------------------------------------------------------------------
# adapter: ShellHookRunner
# ---------------------------------------------------------------------------


def test_runner_runs_command_with_payload_on_stdin_and_env(tmp_path):
    stdin_file = tmp_path / "stdin.json"
    tool_file = tmp_path / "tool.txt"
    spec = HookSpec(
        event="PostToolUse",
        action_type="command",
        command=f'cat > "{stdin_file}"; printf "%s" "$OPENCOLLAB_TOOL" > "{tool_file}"',
    )
    runner = ShellHookRunner((spec,))
    payload = {"hook_event_name": "PostToolUse", "tool": "file_write", "aid": 7}

    outcome = asyncio.run(runner.fire("PostToolUse", payload))

    assert outcome.allow is True
    assert json.loads(stdin_file.read_text()) == payload
    assert tool_file.read_text() == "file_write"


def test_runner_does_not_run_unmatched_command(tmp_path):
    sentinel = tmp_path / "ran"
    spec = HookSpec(
        event="PostToolUse",
        action_type="command",
        command=f'touch "{sentinel}"',
        matcher="file_*",
    )
    runner = ShellHookRunner((spec,))
    # tool 'bash' does not match 'file_*'
    asyncio.run(runner.fire("PostToolUse", {"hook_event_name": "PostToolUse", "tool": "bash"}))
    assert not sentinel.exists()


def test_runner_swallows_nonzero_exit():
    runner = ShellHookRunner((HookSpec(event="Stop", action_type="command", command="exit 3"),))
    # Must not raise.
    outcome = asyncio.run(runner.fire("Stop", {"hook_event_name": "Stop"}))
    assert outcome.allow is True


def test_runner_kills_on_timeout_without_raising():
    spec = HookSpec(event="Stop", action_type="command", command="sleep 5", timeout=0.2)
    runner = ShellHookRunner((spec,))

    async def _run():
        return await asyncio.wait_for(runner.fire("Stop", {"hook_event_name": "Stop"}), timeout=2.0)

    # Returns well under the sleep duration and does not raise.
    outcome = asyncio.run(_run())
    assert outcome.allow is True


def _delayed_sentinel_command(started, finished) -> str:
    code = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.6); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    child = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    return f"{child} & wait"


def test_runner_timeout_kills_descendant_process_group(tmp_path):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=_delayed_sentinel_command(started, finished),
        timeout=0.2,
    )

    asyncio.run(ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"}))

    assert started.exists()
    asyncio.run(asyncio.sleep(0.7))
    assert not finished.exists()


def test_runner_cleans_background_group_after_shell_leader_exits(tmp_path):
    finished = tmp_path / "background-finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.6); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} &"
    runner = ShellHookRunner(
        (HookSpec(event="Stop", action_type="command", command=command),)
    )

    outcome = asyncio.run(
        runner.fire("Stop", {"hook_event_name": "Stop"})
    )

    assert outcome.allow is True
    assert runner.cleanup_quiesced is True
    threading.Event().wait(0.7)
    assert not finished.exists()


def test_runner_caller_cancellation_kills_descendant_process_group(tmp_path):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=_delayed_sentinel_command(started, finished),
        timeout=5.0,
    )

    async def _run() -> None:
        task = asyncio.create_task(
            ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"})
        )
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        assert started.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.7)

    asyncio.run(_run())
    assert not finished.exists()


def test_runner_cancellation_at_spawn_boundary_owns_descendant(
    tmp_path,
    monkeypatch,
):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=_delayed_sentinel_command(started, finished),
        timeout=5.0,
    )
    real_spawn = hooks_adapter.subprocess.Popen
    created = threading.Event()
    release = threading.Event()

    def delayed_spawn(*args, **kwargs):
        proc = real_spawn(*args, **kwargs)
        for _ in range(100):
            if started.exists():
                break
            threading.Event().wait(0.01)
        assert started.exists()
        created.set()
        release.wait()
        return proc

    monkeypatch.setattr(hooks_adapter.subprocess, "Popen", delayed_spawn)

    async def _run() -> None:
        task = asyncio.create_task(
            ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"})
        )
        assert await asyncio.to_thread(created.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.7)

    asyncio.run(_run())
    assert started.exists()
    assert not finished.exists()


def test_runner_cancellation_does_not_wait_for_stuck_spawn(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def stuck_spawn(*_args, **_kwargs):
        started.set()
        release.wait()
        raise OSError("late spawn failure")

    monkeypatch.setattr(hooks_adapter.subprocess, "Popen", stuck_spawn)

    async def _run() -> None:
        task = asyncio.create_task(
            ShellHookRunner((_spec(event="Stop"),)).fire(
                "Stop",
                {"hook_event_name": "Stop"},
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_runner_spawn_timeout_returns_while_late_spawn_is_owned(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def stuck_spawn(*_args, **_kwargs):
        started.set()
        release.wait()
        raise OSError("late spawn failure")

    monkeypatch.setattr(hooks_adapter.subprocess, "Popen", stuck_spawn)

    async def _run() -> None:
        spec = HookSpec(
            event="Stop",
            action_type="command",
            command="true",
            timeout=0.05,
        )
        outcome = await asyncio.wait_for(
            ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"}),
            timeout=0.2,
        )
        assert outcome.allow is True
        assert started.is_set()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_late_spawn_worker_cleans_after_event_loop_closes(tmp_path, monkeypatch):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    release = threading.Event()
    real_spawn = hooks_adapter.subprocess.Popen

    def delayed_return(*args, **kwargs):
        proc = real_spawn(*args, **kwargs)
        for _ in range(100):
            if started.exists():
                break
            threading.Event().wait(0.01)
        assert started.exists()
        release.wait()
        return proc

    monkeypatch.setattr(hooks_adapter.subprocess, "Popen", delayed_return)
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=_delayed_sentinel_command(started, finished),
        timeout=0.05,
    )

    outcome = asyncio.run(
        ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"})
    )
    assert outcome.allow is True
    release.set()
    threading.Event().wait(0.8)
    assert not finished.exists()


def test_cancel_then_interpreter_exit_waits_for_non_daemon_cleanup(tmp_path):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(0.6); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} & wait"
    )
    script = f"""
import asyncio
from pathlib import Path
from opencollab.adapters.hooks import ShellHookRunner
from opencollab.domain.hooks import HookSpec

started = Path({str(started)!r})

async def main():
    spec = HookSpec(event="Stop", action_type="command", command={command!r}, timeout=5.0)
    task = asyncio.create_task(
        ShellHookRunner((spec,)).fire("Stop", {{"hook_event_name": "Stop"}})
    )
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

asyncio.run(main())
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0
    threading.Event().wait(0.8)
    assert not finished.exists()


def test_runner_serializes_payload_before_starting_command(tmp_path):
    sentinel = tmp_path / "started"
    payload = {"hook_event_name": "Stop"}
    payload["cycle"] = payload
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=f'touch "{sentinel}"',
    )

    outcome = asyncio.run(ShellHookRunner((spec,)).fire("Stop", payload))

    assert outcome.allow is True
    assert not sentinel.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_hook_spec_rejects_unbounded_or_nonpositive_timeout(timeout):
    with pytest.raises(ValueError, match="finite positive"):
        HookSpec(event="Stop", action_type="command", command="true", timeout=timeout)


def test_runner_rechecks_mutated_invalid_timeout_before_spawn(tmp_path):
    sentinel = tmp_path / "started"
    spec = HookSpec(
        event="Stop",
        action_type="command",
        command=f'touch "{sentinel}"',
    )
    object.__setattr__(spec, "timeout", float("inf"))

    outcome = asyncio.run(
        ShellHookRunner((spec,)).fire("Stop", {"hook_event_name": "Stop"})
    )

    assert outcome.allow is True
    assert not sentinel.exists()


def test_runner_cleans_up_after_communicate_transport_error(monkeypatch):
    class FakeProcess:
        pid = 912345
        returncode = None
        reaped = False

        def communicate(self, _input, timeout):
            raise OSError("transport failed")

        def wait(self, timeout):
            self.returncode = 1
            self.reaped = True
            return 1

    proc = FakeProcess()

    def fake_spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(hooks_adapter.subprocess, "Popen", fake_spawn)
    monkeypatch.setattr(hooks_adapter.os, "killpg", lambda *_args: None)

    outcome = asyncio.run(
        ShellHookRunner((_spec(event="Stop"),)).fire(
            "Stop",
            {"hook_event_name": "Stop"},
        )
    )

    assert outcome.allow is True
    assert proc.reaped is True


def test_hook_cleanup_permission_error_does_not_escape(monkeypatch):
    class FakeProcess:
        pid = 923456
        returncode = None

        def wait(self, timeout):
            self.returncode = 1
            return 1

    def deny_signal(*_args):
        raise PermissionError("denied")

    monkeypatch.setattr(hooks_adapter.os, "killpg", deny_signal)

    assert hooks_adapter._terminate_process_tree(FakeProcess()) is False


def test_windows_group_configuration_and_taskkill_tree(monkeypatch):
    commands = []

    def fake_run(*args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(hooks_adapter.os, "name", "nt")
    monkeypatch.setattr(hooks_adapter.subprocess, "run", fake_run)

    kwargs = hooks_adapter._subprocess_group_kwargs()
    assert kwargs["creationflags"] != 0
    assert hooks_adapter._taskkill_windows_tree(934567) is True
    assert commands[0][0][0][:5] == ["taskkill", "/PID", "934567", "/T", "/F"]


def test_runner_reserved_action_type_raises_when_matched():
    spec = HookSpec(event="Stop", action_type="agent", command="ignored")
    runner = ShellHookRunner((spec,))
    with pytest.raises(NotImplementedError, match="agent"):
        asyncio.run(runner.fire("Stop", {"hook_event_name": "Stop"}))


# ---------------------------------------------------------------------------
# config: parsing
# ---------------------------------------------------------------------------


_TEAM_WITH_HOOKS = """\
roles:
  lead:
    tools: [bash]
    prompt: |
      Lead.
hooks:
  PostToolUse:
    - matcher: file_write
      command: "ruff format ."
      timeout: 10
  Stop:
    - command: "echo done"
"""


def _write_team(tmp_path, monkeypatch, text):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(text)
    monkeypatch.setenv("OPENCOLLAB_TEAM_FILE", str(configs / "team.yaml"))


def test_config_parses_hooks(tmp_path, monkeypatch):
    _write_team(tmp_path, monkeypatch, _TEAM_WITH_HOOKS)
    cfg = load_team_config(str(tmp_path))

    by_event = {s.event: s for s in cfg.hooks}
    assert set(by_event) == {"PostToolUse", "Stop"}
    assert by_event["PostToolUse"].matcher == "file_write"
    assert by_event["PostToolUse"].command == "ruff format ."
    assert by_event["PostToolUse"].timeout == 10
    assert by_event["Stop"].matcher is None
    assert by_event["Stop"].action_type == "command"


def test_config_unknown_event_raises(tmp_path, monkeypatch):
    bad = "roles:\n  lead:\n    tools: [bash]\n    prompt: x\nhooks:\n  Bogus:\n    - command: echo\n"
    _write_team(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="Unknown hook event"):
        load_team_config(str(tmp_path))


def test_config_unknown_action_type_raises(tmp_path, monkeypatch):
    bad = (
        "roles:\n  lead:\n    tools: [bash]\n    prompt: x\n"
        "hooks:\n  Stop:\n    - command: echo\n      type: banana\n"
    )
    _write_team(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="Unknown hook action type"):
        load_team_config(str(tmp_path))


@pytest.mark.parametrize("timeout", [".inf", ".nan", "0", "-1"])
def test_config_rejects_unbounded_or_nonpositive_hook_timeout(
    tmp_path,
    monkeypatch,
    timeout,
):
    team = (
        "roles:\n  lead:\n    tools: [bash]\n    prompt: x\n"
        f"hooks:\n  Stop:\n    - command: echo\n      timeout: {timeout}\n"
    )
    _write_team(tmp_path, monkeypatch, team)
    with pytest.raises(ValueError):
        load_team_config(str(tmp_path))


def test_config_reserved_action_type_parses(tmp_path, monkeypatch):
    team = (
        "roles:\n  lead:\n    tools: [bash]\n    prompt: x\n"
        "hooks:\n  Stop:\n    - command: echo\n      type: agent\n"
    )
    _write_team(tmp_path, monkeypatch, team)
    cfg = load_team_config(str(tmp_path))
    assert cfg.hooks[0].action_type == "agent"


def test_config_without_hooks_is_empty(tmp_path, monkeypatch):
    _write_team(tmp_path, monkeypatch, "roles:\n  lead:\n    tools: [bash]\n    prompt: x\n")
    assert load_team_config(str(tmp_path)).hooks == ()


# ---------------------------------------------------------------------------
# bootstrap: wiring + eval gating
# ---------------------------------------------------------------------------


def _cfg():
    return {"model": "gpt-4o", "provider": "openai", "api_key": "test-key", "base_url": None, "budget": 100_000}


def _scheduler_with_stop_hook(tmp_path, monkeypatch, *, enable_hooks):
    sentinel = tmp_path / "stop-fired"
    team = (
        f"roles:\n  lead:\n    tools: [bash]\n    prompt: x\nhooks:\n  Stop:\n    - command: 'touch \"{sentinel}\"'\n"
    )
    _write_team(tmp_path, monkeypatch, team)
    ctx = build_runtime_context(str(tmp_path), _cfg(), trace=False)
    scheduler = build_scheduler(ctx, use_worktrees=False, interactive=False, auto_save=False, enable_hooks=enable_hooks)
    return scheduler, sentinel


def test_wiring_fires_hook_on_team_bus(tmp_path, monkeypatch):
    scheduler, sentinel = _scheduler_with_stop_hook(tmp_path, monkeypatch, enable_hooks=True)
    bus = scheduler._event_sink
    assert any(isinstance(t, HookEventSubscriber) for t in bus._targets)

    asyncio.run(bus.emit(SchedulerEvent(type="agent_completed", data={"aid": 0, "parent_aid": None})))
    assert sentinel.exists()


def test_wiring_disabled_when_enable_hooks_false(tmp_path, monkeypatch):
    scheduler, sentinel = _scheduler_with_stop_hook(tmp_path, monkeypatch, enable_hooks=False)
    bus = scheduler._event_sink
    assert not any(isinstance(t, HookEventSubscriber) for t in bus._targets)

    asyncio.run(bus.emit(SchedulerEvent(type="agent_completed", data={"aid": 0, "parent_aid": None})))
    assert not sentinel.exists()
