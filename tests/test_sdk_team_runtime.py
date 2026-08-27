"""SDK team-mode delegation and cleanup contract tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sdk_runtime import (
    OpenCollab,
    ProgrammaticLifecycleError,
    ProgrammaticResult,
    programmatic,
    sdk_client,
)

from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.domain.session import SessionPhase


async def test_team_config_preflight_does_not_claim_artifact_directory(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "team-run"

    with pytest.raises(ValueError, match="team config does not exist"):
        await OpenCollab(tmp_path).team(
            "solve it",
            config=tmp_path / "missing-team.yaml",
            artifacts=artifacts,
            trace=False,
            use_worktrees=False,
        )

    assert not artifacts.exists() or not any(artifacts.iterdir())


async def test_team_is_first_class_and_passes_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = {}

    async def fake_run_team(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(
            output="team done",
            status="completed",
            reason=None,
            tokens=11,
            artifacts=None,
            metrics={"steps": 2, "sessions": 3},
        )

    monkeypatch.setattr(sdk_client, "run_team", fake_run_team)
    config = tmp_path / "team.yaml"
    result = await OpenCollab(tmp_path, config={"budget": 90}).team(
        "solve it",
        config=config,
        cleanup_timeout=3.0,
        use_worktrees=False,
    )

    assert result.output == "team done"
    assert result.tokens == 11
    assert captured["team_config_path"] == config.resolve()
    assert captured["max_tokens"] == 90
    assert captured["cleanup_timeout"] == 3.0
    assert captured["use_worktrees"] is False


async def test_team_defaults_leave_the_experiment_switches_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Product behaviour must not move: an SDK team run that says nothing still
    # seats no roster up front and still hands out no unsandboxed shell.
    captured = {}

    async def fake_run_team(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(
            output="team done",
            status="completed",
            reason=None,
            tokens=1,
            artifacts=None,
            metrics={},
        )

    monkeypatch.setattr(sdk_client, "run_team", fake_run_team)
    await OpenCollab(tmp_path).team("solve it", use_worktrees=False)

    assert captured["prebuild_team"] is False
    assert captured["allow_unisolated_shell"] is None


async def test_team_forwards_the_two_experiment_switches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An unattended experiment needs a declared roster whose agents can run
    # git. Both halves have to survive the trip from the SDK to build_scheduler.
    captured = {}

    async def fake_run_team(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(
            output="team done",
            status="completed",
            reason=None,
            tokens=1,
            artifacts=None,
            metrics={},
        )

    monkeypatch.setattr(sdk_client, "run_team", fake_run_team)
    await OpenCollab(tmp_path).team(
        "solve it",
        use_worktrees=False,
        prebuild_team=True,
        allow_unisolated_shell=True,
    )

    assert captured["prebuild_team"] is True
    assert captured["allow_unisolated_shell"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"prebuild_team": "yes"}, "prebuild_team"),
        ({"allow_unisolated_shell": "yes"}, "allow_unisolated_shell"),
    ),
)
async def test_team_rejects_non_boolean_switches_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kwargs: dict,
    message: str,
) -> None:
    called = False

    async def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(sdk_client, "run_team", should_not_run)

    with pytest.raises(ValueError, match=message):
        await OpenCollab(tmp_path).team("solve it", **kwargs)

    assert not called


async def test_shared_team_runtime_forwards_switches_to_build_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # programmatic.run_team is a forwarder; programmatic_team.run_team is what
    # calls build_scheduler. This pins the whole chain, including the part that
    # must stay fixed: an SDK run has no human, so interactive stays False even
    # when the shell is on.
    captured = {}

    class FakeScheduler:
        used_tokens = 0
        table = SimpleNamespace(entries={0: object()})
        lead_session = SimpleNamespace(
            phase=SimpleNamespace(value="done"),
            state=SimpleNamespace(terminal_reason="completed"),
            step_count=1,
        )

        async def run(self, _prompt: str) -> str:
            return "done"

        async def cleanup(self, *, cleanup_timeout: float) -> None:
            return None

    def fake_build_scheduler(_ctx, **kwargs):
        captured.update(kwargs)
        return FakeScheduler()

    monkeypatch.setattr(programmatic, "build_scheduler", fake_build_scheduler)
    await programmatic.run_team(
        prompt="solve",
        config={"model": "model", "provider": "openai", "budget": 50},
        workspace=str(tmp_path),
        team_config_path=None,
        max_tokens=50,
        timeout=None,
        artifacts=None,
        trace=False,
        use_worktrees=False,
        prebuild_team=True,
        allow_unisolated_shell=True,
    )

    assert captured["interactive"] is False
    assert captured["prebuild_team"] is True
    assert captured["allow_unisolated_shell"] is True


async def test_team_rejects_invalid_cleanup_timeout_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    async def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(sdk_client, "run_team", should_not_run)

    with pytest.raises(ValueError, match="cleanup_timeout"):
        await OpenCollab(tmp_path).team(
            "solve it",
            cleanup_timeout=float("nan"),
        )

    assert not called


async def test_shared_team_runtime_always_cleans_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeScheduler:
        def __init__(self) -> None:
            self.cleaned = False
            self.cleanup_timeout = None
            self.used_tokens = 8
            self.table = SimpleNamespace(entries={0: object()})
            self.lead_session = SimpleNamespace(
                phase=SimpleNamespace(value="done"),
                state=SimpleNamespace(terminal_reason="completed"),
                step_count=2,
            )

        async def run(self, prompt: str) -> str:
            assert prompt == "solve"
            return "done"

        async def cleanup(self, *, cleanup_timeout: float) -> None:
            self.cleanup_timeout = cleanup_timeout
            self.cleaned = True

    scheduler = FakeScheduler()
    monkeypatch.setattr(programmatic, "build_scheduler", lambda *_args, **_kwargs: scheduler)
    result = await programmatic.run_team(
        prompt="solve",
        config={"model": "model", "provider": "openai", "budget": 50},
        workspace=str(tmp_path),
        team_config_path=None,
        max_tokens=50,
        timeout=None,
        cleanup_timeout=3.0,
        artifacts=None,
        trace=False,
        use_worktrees=False,
    )

    assert scheduler.cleaned
    assert scheduler.cleanup_timeout == 3.0
    assert result.status == "completed"
    assert result.tokens == 8
    assert result.metrics == {"steps": 2, "sessions": 1}


@pytest.mark.parametrize(
    ("phase", "terminal_reason", "partial_answer", "expected_status", "expected_reason"),
    [
        (
            SessionPhase.STOPPED,
            "token budget exhausted",
            "partial answer",
            "stopped",
            "token budget exhausted",
        ),
        (
            SessionPhase.ERROR,
            "provider failed",
            "partial answer",
            "failed",
            "provider failed",
        ),
        (SessionPhase.STOPPED, None, None, "stopped", "stopped"),
    ],
)
async def test_team_result_preserves_scheduler_turn_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: SessionPhase,
    terminal_reason: str | None,
    partial_answer: str | None,
    expected_status: str,
    expected_reason: str,
) -> None:
    turn_error = SchedulerTurnError(
        0,
        phase,
        terminal_reason,
        partial_answer,
    )

    class FakeScheduler:
        cleaned = False
        used_tokens = 8
        table = SimpleNamespace(entries={0: object()})
        lead_session = SimpleNamespace(
            phase=phase,
            state=SimpleNamespace(terminal_reason=terminal_reason),
            step_count=2,
        )

        async def run(self, _prompt: str) -> str:
            raise turn_error

        async def cleanup(self, *, cleanup_timeout: float) -> None:
            assert cleanup_timeout > 0
            self.cleaned = True

    scheduler = FakeScheduler()
    monkeypatch.setattr(
        programmatic,
        "build_scheduler",
        lambda *_args, **_kwargs: scheduler,
    )

    result = await programmatic.run_team(
        prompt="solve",
        config={"model": "model", "provider": "openai", "budget": 50},
        workspace=str(tmp_path),
        team_config_path=None,
        max_tokens=50,
        timeout=None,
        artifacts=None,
        trace=False,
        use_worktrees=False,
    )

    assert scheduler.cleaned is True
    assert result.status == expected_status
    assert result.output == partial_answer
    assert result.reason == expected_reason
    assert result.error is turn_error


async def test_team_result_exposes_sanitized_child_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = SimpleNamespace(
        agent=SimpleNamespace(name="reviewer"),
        state=SimpleNamespace(
            phase=SimpleNamespace(value="error"),
            terminal_reason="ProviderFailure: private provider detail",
        ),
    )

    class FakeScheduler:
        used_tokens = 8
        table = SimpleNamespace(entries={0: object(), 1: child})
        lead_session = SimpleNamespace(
            phase=SimpleNamespace(value="done"),
            state=SimpleNamespace(terminal_reason="completed"),
            step_count=2,
        )

        async def run(self, _prompt: str) -> str:
            return "done"

        async def cleanup(self, *, cleanup_timeout: float) -> None:
            assert cleanup_timeout > 0

    monkeypatch.setattr(
        programmatic,
        "build_scheduler",
        lambda *_args, **_kwargs: FakeScheduler(),
    )

    internal = await programmatic.run_team(
        prompt="solve",
        config={"model": "model", "provider": "openai", "budget": 50},
        workspace=str(tmp_path),
        team_config_path=None,
        max_tokens=50,
        timeout=None,
        artifacts=None,
        trace=False,
        use_worktrees=False,
    )
    public = sdk_client._public_result(internal)

    assert public.status == "completed"
    assert public.agent_failures == (
        {
            "label": "reviewer",
            "exception_type": "ProviderFailure",
            "status_code": None,
            "provider_error_type": None,
        },
    )
    assert "private provider detail" not in repr(public)


@pytest.mark.parametrize(
    ("cleanup_fails", "trace_fails"),
    ((True, False), (False, True), (True, True)),
)
async def test_team_lifecycle_failure_preserves_execution_root_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_fails: bool,
    trace_fails: bool,
) -> None:
    primary = ValueError("run-root-cause")
    cleanup_failure = OSError("cleanup-secondary")
    trace_failure = RuntimeError("trace-secondary")

    class FakeScheduler:
        async def run(self, _prompt: str) -> str:
            raise primary

        async def cleanup(self, *, cleanup_timeout: float) -> None:
            assert cleanup_timeout > 0
            if cleanup_fails:
                raise cleanup_failure

    monkeypatch.setattr(
        programmatic,
        "build_scheduler",
        lambda *_args, **_kwargs: FakeScheduler(),
    )
    monkeypatch.setattr(
        programmatic,
        "_close_tracer",
        lambda _tracer: trace_failure if trace_fails else None,
    )

    with pytest.raises(ProgrammaticLifecycleError) as caught:
        await programmatic.run_team(
            prompt="solve",
            config={"model": "model", "provider": "openai", "budget": 50},
            workspace=str(tmp_path),
            team_config_path=None,
            max_tokens=50,
            timeout=None,
            artifacts=None,
            trace=False,
            use_worktrees=False,
        )

    assert caught.value.__cause__ is primary
    notes = getattr(primary, "__notes__", ())
    if cleanup_fails:
        assert any("cleanup-secondary" in note for note in notes)
    if trace_fails:
        assert any("trace-secondary" in note for note in notes)


def test_tool_presets_are_small_fresh_and_named() -> None:
    read_tools = programmatic.resolve_tools("read")
    assert tuple(tool.name for tool in read_tools) == (
        "file_read",
        "grep",
        "git_diff",
    )
    assert read_tools[0] is not programmatic.resolve_tools("read")[0]
    with pytest.raises(ValueError, match="unknown tool preset"):
        programmatic.resolve_tools("everything")
