"""Tests for the minimal (phase-1, observe-only) hook system.

Covers the four layers the feature touches: domain matching policy, the
application event→hook subscriber, the adapter shell runner, the config parser,
and the bootstrap wiring (including the eval-gating switch).
"""

from __future__ import annotations

import asyncio
import json

import pytest

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
