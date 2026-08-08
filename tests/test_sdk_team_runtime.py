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
