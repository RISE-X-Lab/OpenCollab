"""Contract tests for the thin SDK-to-bootstrap delegation layer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap.agent_runtime import AgentRuntimeLifecycleError, AgentRuntimeResult
from opencollab.bootstrap.workflow_runtime import WorkflowDeadlineExceeded, WorkflowLifecycleError
from opencollab.sdk import (
    AgentRunBudget,
    AgentRunLifecycleError,
    AgentRunRequest,
    AgentRunTimeoutError,
    ExecResult,
    ExecutionEnvironment,
    OpenCollabRuntime,
    RunBudget,
    RuntimeConfig,
    WorkflowManifestError,
    WorkflowRunLifecycleError,
    WorkflowRunRequest,
    WorkflowRunTimeoutError,
    workflow,
)
from opencollab.sdk import runtime as sdk_runtime


@workflow(name="sample", description="sample workflow")
async def sample_workflow(_ctx, args):
    return args


class PureEnvironment:
    workspace = "/container/workspace"
    host_workspace = None
    source_workspace = "/host/source"
    local_filesystem = False
    process_isolated = True

    def __init__(self, *, block_cleanup: bool = False) -> None:
        self._revoked = False
        self.abort_calls = 0
        self.cleanup_calls = 0
        self.block_cleanup = block_cleanup
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    @property
    def revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        self._revoked = True

    async def exec_cmd(self, _cmd: str, timeout: float = 120.0) -> ExecResult:
        return ExecResult(0, "", "")

    async def read_file(self, _path: str) -> str:
        return ""

    async def write_file(self, _path: str, _content: str) -> None:
        return None

    async def write_temp_file(self, _content: str, *, prefix: str, suffix: str = ".tmp") -> str:
        return f"/tmp/{prefix}{suffix}"

    async def remove_file(self, _path: str) -> None:
        return None

    async def abort(self) -> None:
        self.abort_calls += 1
        self.revoke()

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.cleanup_started.set()
        if self.block_cleanup:
            await self.cleanup_release.wait()


class CancellationResistantEnvironment(PureEnvironment):
    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.cleanup_started.set()
        while not self.cleanup_release.is_set():
            try:
                await self.cleanup_release.wait()
            except asyncio.CancelledError:
                continue


def _workflow_request(tmp_path: Path, **overrides) -> WorkflowRunRequest:
    values = {
        "workflow": sample_workflow,
        "config": RuntimeConfig(model="model", provider="provider"),
        "inputs": {"value": 3},
        "artifact_dir": tmp_path / "run",
    }
    values.update(overrides)
    return WorkflowRunRequest(**values)


async def test_workflow_runtime_uses_real_bootstrap_and_manifest(tmp_path) -> None:
    result = await OpenCollabRuntime().run_workflow(_workflow_request(tmp_path))
    assert result.output == {"value": 3}
    assert result.tokens_spent == 0
    assert result.session_count == 0
    assert result.manifest_path is not None and result.manifest_path.is_file()


async def test_workflow_runtime_delegates_once_and_reads_manifest(monkeypatch, tmp_path) -> None:
    captured = {}

    async def fake_bootstrap(workflow_value, inputs, **kwargs):
        captured.update({"workflow": workflow_value, "inputs": inputs, **kwargs})
        Path(kwargs["save_dir"], "workflow.json").write_text(
            json.dumps({"tokens_spent": 17, "sessions": 2}), encoding="utf-8"
        )
        return {"answer": 4}

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_bootstrap)
    result = await OpenCollabRuntime().run_workflow(
        _workflow_request(tmp_path, budget=RunBudget(max_tokens=100, max_concurrency=3))
    )
    assert result.output == {"answer": 4}
    assert result.tokens_spent == 17
    assert captured["workflow"] is sample_workflow
    assert captured["inputs"] == {"value": 3}
    assert captured["budget"] == 100
    assert captured["max_concurrency"] == 3


async def test_workflow_runtime_separates_execution_and_source_paths(monkeypatch, tmp_path) -> None:
    captured = {}
    environment = PureEnvironment()

    async def fake_bootstrap(*_args, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_bootstrap)
    result = await OpenCollabRuntime().run_workflow(
        _workflow_request(tmp_path, artifact_dir=None, environment=environment)
    )
    assert isinstance(environment, ExecutionEnvironment)
    assert result.output == "done"
    assert captured["workspace"] == "/container/workspace"
    assert captured["source_root"] == "/host/source"
    assert environment.cleanup_calls == 0


async def test_owned_workflow_cleanup_survives_double_cancellation(monkeypatch, tmp_path) -> None:
    environment = PureEnvironment(block_cleanup=True)

    async def fake_bootstrap(*_args, **_kwargs):
        return "done"

    monkeypatch.setattr(sdk_runtime, "LocalEnvironment", lambda _workspace: environment)
    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_bootstrap)
    owner = asyncio.create_task(
        OpenCollabRuntime().run_workflow(
            _workflow_request(tmp_path, artifact_dir=None)
        )
    )
    await environment.cleanup_started.wait()
    owner.cancel()
    owner.cancel()
    environment.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert environment.cleanup_calls == 1


async def test_owned_workflow_reports_cleanup_that_misses_bounded_deadline(
    monkeypatch, tmp_path
) -> None:
    environment = CancellationResistantEnvironment()

    async def fake_bootstrap(*_args, **_kwargs):
        return "done"

    monkeypatch.setattr(sdk_runtime, "LocalEnvironment", lambda _workspace: environment)
    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_bootstrap)
    request = _workflow_request(
        tmp_path,
        artifact_dir=None,
        budget=RunBudget(cleanup_timeout_seconds=0.01),
    )

    with pytest.raises(WorkflowRunLifecycleError, match="cleanup failed"):
        await OpenCollabRuntime().run_workflow(request)

    environment.cleanup_release.set()
    await asyncio.sleep(0)
    assert environment.cleanup_calls == 1


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (WorkflowDeadlineExceeded("wall"), WorkflowRunTimeoutError),
        (WorkflowLifecycleError("cleanup"), WorkflowRunLifecycleError),
    ],
)
async def test_workflow_runtime_maps_bootstrap_lifecycle_errors(
    monkeypatch, tmp_path, raised, expected
) -> None:
    async def fail(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fail)
    with pytest.raises(expected):
        await OpenCollabRuntime().run_workflow(
            _workflow_request(tmp_path, artifact_dir=None, budget=RunBudget(timeout_seconds=1))
        )


async def test_workflow_runtime_preserves_inner_provider_timeout(monkeypatch, tmp_path) -> None:
    inner = asyncio.TimeoutError("provider timeout")

    async def fail(*_args, **_kwargs):
        raise inner

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fail)
    with pytest.raises(asyncio.TimeoutError) as captured:
        await OpenCollabRuntime().run_workflow(
            _workflow_request(tmp_path, artifact_dir=None, budget=RunBudget(timeout_seconds=1))
        )
    assert captured.value is inner


async def test_workflow_runtime_rejects_invalid_or_reused_artifacts(monkeypatch, tmp_path) -> None:
    async def fake_bootstrap(*_args, **kwargs):
        Path(kwargs["save_dir"], "workflow.json").write_text("{}", encoding="utf-8")
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_bootstrap)
    request = _workflow_request(tmp_path)
    with pytest.raises(WorkflowManifestError, match="tokens_spent"):
        await OpenCollabRuntime().run_workflow(request)
    with pytest.raises(sdk_runtime.InvalidSDKRequestError, match="evidence|claimed"):
        await OpenCollabRuntime().run_workflow(request)


async def test_artifact_claim_excludes_concurrent_runs(monkeypatch, tmp_path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*_args, **kwargs):
        entered.set()
        await release.wait()
        Path(kwargs["save_dir"], "workflow.json").write_text(
            '{"tokens_spent":0,"sessions":0}', encoding="utf-8"
        )
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", blocked)
    request = _workflow_request(tmp_path)
    first = asyncio.create_task(OpenCollabRuntime().run_workflow(request))
    await entered.wait()
    with pytest.raises(sdk_runtime.InvalidSDKRequestError, match="claimed"):
        await OpenCollabRuntime().run_workflow(request)
    release.set()
    assert (await first).output == "done"


def _agent_internal(
    *,
    outcome="completed",
    output="finished",
    error=None,
    persistence_errors=(),
) -> AgentRuntimeResult:
    return AgentRuntimeResult(
        output=output,
        outcome=outcome,
        error=error,
        phase="done",
        terminal_reason="completed",
        tokens_spent=6,
        step_count=1,
        cleanup_quiesced=True,
        persistence_errors=persistence_errors,
    )


async def test_agent_runtime_delegates_and_maps_result(monkeypatch, tmp_path) -> None:
    captured = {}

    async def fake_agent(**kwargs):
        captured.update(kwargs)
        Path(kwargs["transcript_path"]).write_text("{}", encoding="utf-8")
        return _agent_internal()

    monkeypatch.setattr(sdk_runtime, "_run_bootstrap_agent", fake_agent)
    request = AgentRunRequest(
        prompt="finish",
        config=RuntimeConfig(model="model", provider="provider"),
        environment=PureEnvironment(),
        artifact_dir=tmp_path / "agent",
        trace=False,
    )
    result = await OpenCollabRuntime().run_agent(request)
    assert result.outcome == "completed"
    assert result.output == "finished"
    assert result.tokens_spent == 6
    assert captured["prompt"] == "finish"
    assert captured["agent"].model == "model"
    assert captured["cleanup_environment"] is False


async def test_agent_timeout_raise_and_return_modes(monkeypatch) -> None:
    async def timed_out(**_kwargs):
        return _agent_internal(outcome="timed_out", output=None, error=TimeoutError("internal"))

    monkeypatch.setattr(sdk_runtime, "_run_bootstrap_agent", timed_out)
    values = {
        "prompt": "block",
        "config": RuntimeConfig(model="model", provider="provider"),
        "budget": AgentRunBudget(timeout_seconds=0.01),
        "environment": PureEnvironment(),
        "trace": False,
    }
    with pytest.raises(AgentRunTimeoutError, match="exceeded"):
        await OpenCollabRuntime().run_agent(AgentRunRequest(**values))
    result = await OpenCollabRuntime().run_agent(AgentRunRequest(**values, failure_mode="return"))
    assert result.outcome == "timed_out"
    assert result.error_type == "AgentRunTimeoutError"
    assert result.cleanup_quiesced


async def test_agent_runtime_maps_lifecycle_failure(monkeypatch) -> None:
    async def fail(**_kwargs):
        raise AgentRuntimeLifecycleError("not quiescent")

    monkeypatch.setattr(sdk_runtime, "_run_bootstrap_agent", fail)
    request = AgentRunRequest(
        prompt="run",
        config=RuntimeConfig(model="model", provider="provider"),
        environment=PureEnvironment(),
        trace=False,
    )
    with pytest.raises(AgentRunLifecycleError, match="quiescent"):
        await OpenCollabRuntime().run_agent(request)


async def test_agent_runtime_requires_final_transcript(monkeypatch, tmp_path) -> None:
    async def fake_agent(**_kwargs):
        return _agent_internal()

    monkeypatch.setattr(sdk_runtime, "_run_bootstrap_agent", fake_agent)
    request = AgentRunRequest(
        prompt="run",
        config=RuntimeConfig(model="model", provider="provider"),
        environment=PureEnvironment(),
        artifact_dir=tmp_path / "agent",
        trace=False,
    )
    with pytest.raises(AgentRunLifecycleError, match="evidence"):
        await OpenCollabRuntime().run_agent(request)


class ReplyLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        return LLMResponse(
            content="finished",
            usage=Usage(input_tokens=4, output_tokens=2),
            finish_reason="stop",
        )


async def test_agent_runtime_real_bootstrap_smoke(tmp_path) -> None:
    result = await OpenCollabRuntime().run_agent(
        AgentRunRequest(
            prompt="finish once",
            config=RuntimeConfig(model="model", provider="provider"),
            llm=ReplyLLM(),
            environment_workdir=str(tmp_path),
            artifact_dir=tmp_path / "agent-run",
            trace=False,
        )
    )
    assert result.output == "finished"
    assert result.phase == "done"
    assert result.terminal_reason == "completed"
    assert result.tokens_spent == 6
    assert result.transcript_path is not None and result.transcript_path.is_file()
