"""Team wind-down failures retain execution evidence and propagate cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.bootstrap import programmatic
from opencollab.domain.session import SessionPhase


async def _run(monkeypatch, tmp_path: Path, *, primary=None, cleanup=None, trace=None, environment=None):
    class Scheduler:
        used_tokens = 123
        table = SimpleNamespace(entries={0: object()})
        lead_session = SimpleNamespace(
            phase=SimpleNamespace(value="done"),
            state=SimpleNamespace(terminal_reason="completed"),
            step_count=7,
        )

        async def run(self, _prompt):
            if primary is not None:
                raise primary
            return "completed answer"

        async def cleanup(self, *, cleanup_timeout):
            assert cleanup_timeout > 0
            if cleanup is not None:
                raise cleanup

    monkeypatch.setattr(programmatic, "build_scheduler", lambda *_args, **_kwargs: Scheduler())
    monkeypatch.setattr(programmatic, "_close_tracer", lambda _tracer: trace)
    return await programmatic.run_team(
        prompt="solve",
        config={"model": "model", "provider": "openai", "budget": 500},
        workspace=str(tmp_path),
        team_config_path=None,
        max_tokens=500,
        timeout=None,
        artifacts=None,
        trace=False,
        use_worktrees=False,
        environment=environment,
    )


@pytest.mark.parametrize("failure_source", ["cleanup", "trace", "both"])
async def test_wind_down_failure_preserves_completed_answer_and_cost(monkeypatch, tmp_path, failure_source):
    cleanup = OSError("cleanup failed") if failure_source != "trace" else None
    trace = RuntimeError("trace failed") if failure_source != "cleanup" else None

    result = await _run(monkeypatch, tmp_path, cleanup=cleanup, trace=trace)

    assert result.output == "completed answer"
    assert result.tokens == 123
    assert result.metrics["steps"] == 7
    assert result.status == "failed"
    assert result.error is (cleanup or trace)
    assert "cleanup or trajectory persistence failed" in result.reason
    assert result.metrics["session_quiesced"] is False
    assert result.metrics["environment_quiesced"] is False
    assert result.metrics["execution_quiesced"] is False
    if cleanup is not None and trace is not None:
        assert any("trace failed" in note for note in getattr(cleanup, "__notes__", ()))


async def test_wind_down_failure_retains_partial_answer_and_original_stop(monkeypatch, tmp_path):
    primary = SchedulerTurnError(0, SessionPhase.STOPPED, "budget exhausted", "partial answer")

    result = await _run(monkeypatch, tmp_path, primary=primary, cleanup=OSError("cleanup failed"))

    assert result.output == "partial answer"
    assert result.tokens == 123
    assert result.status == "stopped"
    assert result.reason == "budget exhausted"
    assert result.error is primary
    assert any("cleanup failed" in note for note in getattr(primary, "__notes__", ()))
    assert result.metrics["execution_quiesced"] is False


async def test_wind_down_failure_does_not_claim_cleanup_of_external_environment(monkeypatch, tmp_path):
    result = await _run(monkeypatch, tmp_path, cleanup=OSError("cleanup failed"), environment=object())

    assert result.metrics["environment_owned"] is False
    assert result.metrics["environment_quiesced"] is None
    assert result.metrics["environment_cleanup_quiesced"] is None
    assert result.metrics["execution_quiesced"] is False


@pytest.mark.parametrize("stage", ["run", "cleanup", "trace"])
async def test_cancellation_during_run_or_wind_down_is_raised(monkeypatch, tmp_path, stage):
    cancellation = asyncio.CancelledError()
    failures = {stage if stage != "run" else "primary": cancellation}

    with pytest.raises(asyncio.CancelledError) as caught:
        await _run(monkeypatch, tmp_path, **failures)

    assert caught.value is cancellation
