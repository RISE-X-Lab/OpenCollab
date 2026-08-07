"""Integration tests for the thin SDK-to-bootstrap delegation layer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab import OpenCollab, RunError, workflow
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow import WorkflowBudgetExceeded
from opencollab.bootstrap import (
    _workflow_runtime_execution as workflow_execution,
)
from opencollab.bootstrap import (
    programmatic,
)
from opencollab.bootstrap.programmatic import ProgrammaticLifecycleError, ProgrammaticResult
from opencollab.sdk import client as sdk_client


class ReplyLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        return LLMResponse(
            content="finished",
            usage=Usage(input_tokens=4, output_tokens=2),
            finish_reason="stop",
        )


async def test_agent_uses_real_hardened_runtime_and_persists_evidence(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "agent-run"
    result = await OpenCollab(tmp_path, model="model", provider="openai").agent(
        "finish once",
        tools=(),
        llm=ReplyLLM(),
        artifacts=artifacts,
        trace=False,
    )

    assert result.ok
    assert result.output == "finished"
    assert result.reason is None
    assert result.tokens == 6
    assert result.metrics == _completed_agent_metrics()
    assert result.artifacts == artifacts.resolve()
    assert (artifacts / ".opencollab-run").read_text() == "claimed\n"
    transcript = json.loads((artifacts / "agent.json").read_text())
    assert transcript["messages"][-1]["content"] == "finished"


async def test_client_resolves_config_once_and_delegates_plain_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(
            output="done",
            status="completed",
            reason=None,
            tokens=7,
            artifacts=kwargs["artifacts"],
            metrics={"steps": 2},
        )

    monkeypatch.setattr(sdk_client, "run_agent", fake_run_agent)
    client = OpenCollab(
        tmp_path,
        model="unit-model",
        provider="openai",
        api_key="private-key",
        config={"budget": 123, "temperature": 0.7},
    )
    result = await client.agent(
        "do it",
        tools="read",
        max_steps=3,
        cleanup_timeout=4.0,
        artifacts=tmp_path / "evidence",
        trace=False,
    )

    assert result.output == "done"
    assert result.metrics == {"steps": 2}
    assert captured["workspace"] == str(tmp_path.resolve())
    assert captured["config"]["model"] == "unit-model"
    assert captured["config"]["api_key"] == "private-key"
    assert captured["max_tokens"] == 123
    assert captured["max_steps"] == 3
    assert captured["cleanup_timeout"] == 4.0
    assert captured["tools"] == "read"
    assert captured["artifacts"] == (tmp_path / "evidence").resolve()
    assert "private-key" not in repr(client)

    await client.agent("legacy", steps=4)
    assert captured["max_steps"] == 4


def _completed_agent_metrics() -> dict[str, object]:
    return {
        "steps": 1,
        "outcome": "completed",
        "phase": "done",
        "terminal_reason": "completed",
        "markup_recovered": 0,
        "session_quiesced": True,
        "environment_owned": True,
        "environment_cleanup_quiesced": True,
        "environment_quiesced": True,
        "cleanup_quiesced": True,
        "execution_quiesced": True,
    }


async def test_agent_does_not_claim_caller_owned_environment_quiescence(
    tmp_path: Path,
) -> None:
    environment = LocalEnvironment(str(tmp_path))
    result = await OpenCollab(
        tmp_path,
        model="model",
        provider="openai",
        environment=environment,
    ).agent(
        "finish once",
        tools=(),
        llm=ReplyLLM(),
        trace=False,
    )

    assert result.status == "completed"
    assert result.metrics["session_quiesced"] is True
    assert result.metrics["environment_owned"] is False
    assert result.metrics["environment_cleanup_quiesced"] is None
    assert result.metrics["environment_quiesced"] is None
    assert result.metrics["cleanup_quiesced"] is None
    assert result.metrics["execution_quiesced"] is None
    assert environment.revoked is False


async def test_agent_validates_controls_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    async def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(sdk_client, "run_agent", should_not_run)
    client = OpenCollab(tmp_path)
    with pytest.raises(ValueError, match="prompt"):
        await client.agent(" ")
    with pytest.raises(ValueError, match="budget"):
        await client.agent("run", budget=True)
    with pytest.raises(ValueError, match="timeout"):
        await client.agent("run", timeout=float("inf"))
    with pytest.raises(ValueError, match="cleanup_timeout"):
        await client.agent("run", cleanup_timeout=0)
    with pytest.raises(ValueError, match="cannot both"):
        await client.agent("run", max_steps=3, steps=3)
    assert not called


async def test_workflow_delegates_evaluation_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = {}

    async def fake_run_workflow(**kwargs):
        captured.update(kwargs)
        return ProgrammaticResult(
            output="done",
            status="completed",
            reason=None,
            tokens=9,
            artifacts=None,
            metrics={
                "steps": 3,
                "sessions": 2,
                "markup_recovered": 1,
                "execution_quiesced": True,
            },
        )

    monkeypatch.setattr(sdk_client, "run_workflow", fake_run_workflow)

    async def plain(_ctx, _inputs):
        return None

    result = await OpenCollab(tmp_path).workflow(
        plain,
        max_steps=60,
        system_prompt="Evaluation system prompt",
        cleanup_timeout=10.0,
    )

    assert result.output == "done"
    assert captured["max_steps"] == 60
    assert captured["system_prompt"] == "Evaluation system prompt"
    assert captured["cleanup_timeout"] == 10.0


async def test_workflow_exposes_sanitized_agent_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_run_workflow(**_kwargs):
        return ProgrammaticResult(
            output="partial",
            status="completed",
            reason=None,
            tokens=9,
            artifacts=None,
            agent_failures=(
                {
                    "label": "reviewer",
                    "exception_type": "ProviderFailure",
                    "status_code": 403,
                    "provider_error_type": "access_terminated_error",
                    "message": "private provider detail",
                },
            ),
        )

    monkeypatch.setattr(sdk_client, "run_workflow", fake_run_workflow)

    async def plain(_ctx, _inputs):
        return None

    result = await OpenCollab(tmp_path).workflow(plain)

    assert result.agent_failures == (
        {
            "label": "reviewer",
            "exception_type": "ProviderFailure",
            "status_code": 403,
            "provider_error_type": "access_terminated_error",
        },
    )
    assert "message" not in result.agent_failures[0]
    assert "private provider detail" not in repr(result)


async def test_workflow_rejects_invalid_evaluation_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    async def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(sdk_client, "run_workflow", should_not_run)

    async def plain(_ctx, _inputs):
        return None

    client = OpenCollab(tmp_path)
    with pytest.raises(ValueError, match="max_steps"):
        await client.workflow(plain, max_steps=0)
    with pytest.raises(ValueError, match="system_prompt"):
        await client.workflow(plain, system_prompt=" ")
    with pytest.raises(ValueError, match="cleanup_timeout"):
        await client.workflow(plain, cleanup_timeout=float("nan"))
    assert not called


async def test_workflow_uses_real_runtime_and_returns_live_metrics(
    tmp_path: Path,
) -> None:
    @workflow(name="echo-flow", description="Echo one value")
    async def echo(_ctx, inputs):
        return {"answer": inputs["value"] + 1}

    artifacts = tmp_path / "workflow-run"
    result = await OpenCollab(tmp_path).workflow(
        echo,
        {"value": 3},
        artifacts=artifacts,
        trace=False,
    )

    assert result.ok
    assert result.output == {"answer": 4}
    assert result.tokens == 0
    assert result.metrics == {
        "steps": 0,
        "sessions": 0,
        "markup_recovered": 0,
        "session_quiesced": True,
        "environment_owned": True,
        "environment_cleanup_quiesced": True,
        "environment_quiesced": True,
        "cleanup_quiesced": True,
        "execution_quiesced": True,
    }
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["workflow"] == "echo-flow"
    assert manifest["args"] == {"value": 3}
    assert manifest["status"] == "completed"
    assert manifest["reason"] is None
    assert manifest["failure_type"] is None
    assert manifest["evidence_complete"] is True


async def test_workflow_rejects_non_json_inputs_before_claiming_artifacts(
    tmp_path: Path,
) -> None:
    invoked = False

    async def plain(_ctx, _inputs):
        nonlocal invoked
        invoked = True
        return "completed before the old late manifest failure"

    inputs = {"path": Path("not-json-serializable")}
    result = await OpenCollab(tmp_path).workflow(plain, inputs, trace=False)
    assert result.output == "completed before the old late manifest failure"
    assert invoked is True

    invoked = False
    artifacts = tmp_path / "workflow-run"
    with pytest.raises(ValueError, match="JSON-serializable"):
        await OpenCollab(tmp_path).workflow(
            plain,
            inputs,
            artifacts=artifacts,
            trace=False,
        )

    assert invoked is False
    assert not artifacts.exists()


async def test_workflow_execution_failure_is_a_result(tmp_path: Path) -> None:
    @workflow
    async def broken(_ctx, _inputs):
        raise ValueError("bad workflow")

    artifacts = tmp_path / "failed-workflow"
    result = await OpenCollab(tmp_path).workflow(
        broken,
        artifacts=artifacts,
        trace=False,
    )
    assert result.status == "failed"
    assert result.reason == "bad workflow"
    assert result.tokens == 0
    assert result.metrics["execution_quiesced"] is True
    assert isinstance(result.error, ValueError)
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["reason"] == "workflow_exception"
    assert manifest["failure_type"] == "ValueError"
    assert manifest["evidence_complete"] is True
    with pytest.raises(RunError, match="bad workflow"):
        result.raise_for_status()


async def test_workflow_timeout_is_a_stopped_result(tmp_path: Path) -> None:
    @workflow
    async def blocked(_ctx, _inputs):
        await asyncio.Event().wait()

    artifacts = tmp_path / "timed-out-workflow"
    result = await OpenCollab(tmp_path).workflow(
        blocked,
        timeout=0.01,
        artifacts=artifacts,
        trace=False,
    )
    assert result.status == "stopped"
    assert result.reason == "timeout"
    assert result.tokens == 0
    assert result.metrics == {
        "steps": 0,
        "sessions": 0,
        "markup_recovered": 0,
        "session_quiesced": True,
        "environment_owned": True,
        "environment_cleanup_quiesced": True,
        "environment_quiesced": True,
        "cleanup_quiesced": True,
        "execution_quiesced": True,
    }
    assert isinstance(result.error, TimeoutError)
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["status"] == "stopped"
    assert manifest["reason"] == "cancelled"
    assert manifest["failure_type"] == "CancelledError"
    assert manifest["evidence_complete"] is True


async def test_workflow_budget_stop_persists_terminal_manifest(
    tmp_path: Path,
) -> None:
    async def exhausted(_ctx, _inputs):
        raise WorkflowBudgetExceeded("budget exhausted")

    artifacts = tmp_path / "budget-workflow"
    result = await OpenCollab(tmp_path).workflow(
        exhausted,
        artifacts=artifacts,
        trace=False,
    )

    assert result.status == "stopped"
    assert result.reason == "budget_exceeded"
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["status"] == "stopped"
    assert manifest["reason"] == "budget_exceeded"
    assert manifest["failure_type"] is None
    assert manifest["evidence_complete"] is True


async def test_user_workflow_status_payload_does_not_fake_a_budget_stop(
    tmp_path: Path,
) -> None:
    @workflow
    async def ordinary(_ctx, _inputs):
        return {"status": "budget_exceeded", "source": "user"}

    result = await OpenCollab(tmp_path).workflow(ordinary, trace=False)

    assert result.status == "completed"
    assert result.reason is None
    assert result.output == {"status": "budget_exceeded", "source": "user"}


async def test_workflow_does_not_claim_caller_owned_environment_quiescence(
    tmp_path: Path,
) -> None:
    environment = LocalEnvironment(str(tmp_path))

    async def plain(_ctx, _inputs):
        return "done"

    result = await OpenCollab(tmp_path, environment=environment).workflow(
        plain,
        trace=False,
    )

    assert result.status == "completed"
    assert result.metrics["session_quiesced"] is True
    assert result.metrics["environment_owned"] is False
    assert result.metrics["environment_cleanup_quiesced"] is None
    assert result.metrics["environment_quiesced"] is None
    assert result.metrics["cleanup_quiesced"] is None
    assert result.metrics["execution_quiesced"] is None
    assert environment.revoked is False


async def test_lifecycle_failure_raises_the_single_public_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail(**_kwargs):
        raise ProgrammaticLifecycleError("cleanup did not quiesce")

    monkeypatch.setattr(sdk_client, "run_workflow", fail)

    async def plain(_ctx, _inputs):
        return None

    with pytest.raises(RunError, match="quiesce"):
        await OpenCollab(tmp_path).workflow(plain)


@pytest.mark.parametrize(
    "failure_point",
    ("cleanup", "manifest", "trace"),
)
async def test_real_workflow_technical_failures_raise_public_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    async def plain(_ctx, _inputs):
        return "done"

    artifacts = tmp_path / failure_point
    if failure_point == "cleanup":

        async def fail_cleanup(*_args, **_kwargs):
            raise RuntimeError("cleanup evidence failed")

        monkeypatch.setattr(
            workflow_execution,
            "_quiesce_and_finalize_workflow_context",
            fail_cleanup,
        )
    elif failure_point == "manifest":
        monkeypatch.setattr(
            workflow_execution,
            "_persist_workflow_manifest",
            lambda *_args, **_kwargs: RuntimeError("manifest evidence failed"),
        )
    else:
        monkeypatch.setattr(
            workflow_execution,
            "_close_tracer_capture",
            lambda _tracer: RuntimeError("trace evidence failed"),
        )

    with pytest.raises(RunError, match="finalized execution evidence"):
        await OpenCollab(tmp_path).workflow(
            plain,
            artifacts=artifacts,
            trace=failure_point == "trace",
        )


async def test_incomplete_trace_upgrades_workflow_error_to_public_lifecycle_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def broken(_ctx, _inputs):
        raise ValueError("workflow failed first")

    monkeypatch.setattr(
        workflow_execution,
        "_close_tracer_capture",
        lambda _tracer: RuntimeError("trace evidence failed"),
    )

    with pytest.raises(RunError, match="finalized execution evidence") as caught:
        await OpenCollab(tmp_path).workflow(
            broken,
            artifacts=tmp_path / "combined-failure",
            trace=True,
        )

    assert isinstance(caught.value.__cause__.__cause__, ValueError)


async def test_artifact_directory_must_be_new_or_empty(tmp_path: Path) -> None:
    artifacts = tmp_path / "existing"
    artifacts.mkdir()
    (artifacts / "keep.txt").write_text("user data")

    async def plain(_ctx, _inputs):
        return None

    with pytest.raises(ValueError, match="new or empty"):
        await OpenCollab(tmp_path).workflow(plain, artifacts=artifacts, trace=False)
    assert (artifacts / "keep.txt").read_text() == "user data"


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
