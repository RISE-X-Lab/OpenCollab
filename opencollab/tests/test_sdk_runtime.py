from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from opencollab.sdk.usage import LLMResponse, Usage


@workflow(name="sample", description="sample workflow")
async def sample_workflow(ctx, args):
    return args


class PureEnvironment:
    workspace = "/container/workspace"
    host_workspace = None
    source_workspace = "/host/source"
    local_filesystem = False
    process_isolated = True

    def __init__(self, *, abort_fails: bool = False, revoke_fails: bool = False) -> None:
        self._revoked = False
        self.abort_fails = abort_fails
        self.revoke_fails = revoke_fails
        self.abort_calls = 0
        self.cleanup_calls = 0

    @property
    def revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        if self.revoke_fails:
            raise RuntimeError("revoke failed")
        self._revoked = True

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        return ExecResult(0, "", "")

    async def read_file(self, path: str) -> str:
        return ""

    async def write_file(self, path: str, content: str) -> None:
        return None

    async def write_temp_file(self, content: str, *, prefix: str, suffix: str = ".tmp") -> str:
        return f"/tmp/{prefix}{suffix}"

    async def remove_file(self, path: str) -> None:
        return None

    async def registered_retirement_paths(self) -> tuple[str, ...]:
        return ()

    async def abort(self) -> None:
        self.abort_calls += 1
        self.revoke()
        if self.abort_fails:
            raise RuntimeError("abort failed")

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


def _request(tmp_path: Path, **kwargs) -> WorkflowRunRequest:
    values = {
        "workflow": sample_workflow,
        "config": RuntimeConfig(model="model", provider="provider"),
        "inputs": {"value": 3},
        "artifact_dir": tmp_path / "run",
    }
    values.update(kwargs)
    return WorkflowRunRequest(**values)


async def test_runtime_executes_through_real_hardened_lifecycle(tmp_path: Path) -> None:
    result = await OpenCollabRuntime().run_workflow(_request(tmp_path))
    assert result.output == {"value": 3}
    assert result.tokens_spent == 0
    assert result.session_count == 0
    assert result.manifest_path is not None
    assert result.manifest_path.is_file()


async def test_runtime_delegates_to_hardened_lifecycle_and_reads_manifest(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    async def fake_hardened(workflow_value, inputs, **kwargs):
        captured.update({"workflow": workflow_value, "inputs": inputs, **kwargs})
        manifest = {"tokens_spent": 17, "sessions": 2}
        (Path(kwargs["save_dir"]) / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")
        return {"answer": 4}

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_hardened)
    result = await OpenCollabRuntime().run_workflow(
        _request(
            tmp_path,
            budget=RunBudget(max_tokens=100, max_concurrency=3, cleanup_timeout_seconds=7),
        )
    )

    assert result.output == {"answer": 4}
    assert result.workflow_name == "sample"
    assert result.tokens_spent == 17
    assert result.session_count == 2
    assert result.manifest_path == tmp_path / "run" / "workflow.json"
    assert captured["workflow"] is sample_workflow
    assert captured["inputs"] == {"value": 3}
    assert captured["budget"] == 100
    assert captured["max_concurrency"] == 3
    assert captured["cleanup_timeout"] == 7


async def test_runtime_accepts_structural_environment_and_separates_source_root(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    environment = PureEnvironment()

    async def fake_hardened(*args, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_hardened)
    request = _request(
        tmp_path,
        artifact_dir=None,
        environment=environment,
        budget=RunBudget(timeout_seconds=1, deadline_margin_seconds=0.25),
    )
    assert isinstance(environment, ExecutionEnvironment)

    result = await OpenCollabRuntime().run_workflow(request)

    assert result.output == "done"
    assert captured["env"] is environment
    assert captured["workspace"] == "/container/workspace"
    assert captured["source_root"] == "/host/source"
    assert captured["deadline_margin_seconds"] == 0.25
    assert 0 < captured["deadline_monotonic"] - time.monotonic() <= 1
    assert environment.cleanup_calls == 0


async def test_runtime_without_artifacts_returns_unknown_metrics(monkeypatch, tmp_path: Path) -> None:
    async def fake_hardened(*args, **kwargs):
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_hardened)
    request = _request(tmp_path, artifact_dir=None)
    result = await OpenCollabRuntime().run_workflow(request)
    assert result.output == "done"
    assert result.tokens_spent is None
    assert result.session_count is None
    assert result.manifest_path is None


async def test_runtime_rejects_invalid_hardened_manifest(monkeypatch, tmp_path: Path) -> None:
    async def fake_hardened(*args, **kwargs):
        (Path(kwargs["save_dir"]) / "workflow.json").write_text("{}", encoding="utf-8")
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fake_hardened)
    with pytest.raises(WorkflowManifestError, match="tokens_spent"):
        await OpenCollabRuntime().run_workflow(_request(tmp_path))


async def test_runtime_rejects_reused_artifact_directory(tmp_path: Path) -> None:
    request = _request(tmp_path)
    await OpenCollabRuntime().run_workflow(request)

    with pytest.raises(sdk_runtime.InvalidSDKRequestError, match="workflow evidence|claimed"):
        await OpenCollabRuntime().run_workflow(request)


async def test_runtime_rejects_artifact_directory_with_unclaimed_stale_evidence(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "run"
    artifact_dir.mkdir()
    stale = artifact_dir / "trajectory.jsonl"
    stale.write_text('{"old": true}\n', encoding="utf-8")

    with pytest.raises(sdk_runtime.InvalidSDKRequestError, match="run evidence"):
        await OpenCollabRuntime().run_workflow(_request(tmp_path))

    assert stale.read_text(encoding="utf-8") == '{"old": true}\n'
    assert not (artifact_dir / sdk_runtime._SDK_ARTIFACT_CLAIM_FILENAME).exists()


async def test_runtime_atomically_rejects_concurrent_artifact_directory_use(monkeypatch, tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_hardened(*args, **kwargs):
        started.set()
        await release.wait()
        manifest = {"tokens_spent": 0, "sessions": 0}
        (Path(kwargs["save_dir"]) / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")
        return "done"

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", blocked_hardened)
    request = _request(tmp_path)
    first = asyncio.create_task(OpenCollabRuntime().run_workflow(request))
    await started.wait()
    with pytest.raises(sdk_runtime.InvalidSDKRequestError, match="claimed"):
        await OpenCollabRuntime().run_workflow(request)
    release.set()
    assert (await first).output == "done"


async def test_runtime_distinguishes_inner_timeout_from_sdk_deadline(monkeypatch, tmp_path: Path) -> None:
    inner = asyncio.TimeoutError("provider timeout")

    async def fail_with_inner_timeout(*args, **kwargs):
        raise inner

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", fail_with_inner_timeout)
    with pytest.raises(asyncio.TimeoutError, match="provider timeout") as captured:
        await OpenCollabRuntime().run_workflow(
            _request(tmp_path, artifact_dir=None, budget=RunBudget(timeout_seconds=1))
        )
    assert captured.value is inner


async def test_runtime_deadline_waits_for_coroutine_cancellation(monkeypatch, tmp_path: Path) -> None:
    cancelled = asyncio.Event()

    async def slow_hardened(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", slow_hardened)
    with pytest.raises(WorkflowRunTimeoutError, match="exceeded"):
        await OpenCollabRuntime().run_workflow(
            _request(tmp_path, artifact_dir=None, budget=RunBudget(timeout_seconds=0.01))
        )
    assert cancelled.is_set()


async def test_runtime_timeout_fails_lifecycle_when_environment_abort_fails(monkeypatch, tmp_path: Path) -> None:
    environment = PureEnvironment(abort_fails=True)

    async def slow_hardened(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", slow_hardened)
    with pytest.raises(WorkflowRunLifecycleError, match="abort"):
        await OpenCollabRuntime().run_workflow(
            _request(
                tmp_path,
                artifact_dir=None,
                environment=environment,
                budget=RunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.01),
            )
        )
    assert environment.revoked


async def test_runtime_timeout_still_terminates_owner_when_environment_revoke_fails(
    monkeypatch, tmp_path: Path
) -> None:
    environment = PureEnvironment(revoke_fails=True)
    cancelled = asyncio.Event()

    async def slow_hardened(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", slow_hardened)
    with pytest.raises(WorkflowRunLifecycleError, match="abort"):
        await OpenCollabRuntime().run_workflow(
            _request(
                tmp_path,
                artifact_dir=None,
                environment=environment,
                budget=RunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.01),
            )
        )
    assert environment.abort_calls >= 1
    assert cancelled.is_set()


async def test_runtime_timeout_is_bounded_when_workflow_swallows_cancellation(monkeypatch, tmp_path: Path) -> None:
    environment = PureEnvironment()
    release = asyncio.Event()

    async def cancellation_resistant(*args, **kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(sdk_runtime, "_run_hardened_workflow", cancellation_resistant)
    started = time.monotonic()
    with pytest.raises(WorkflowRunLifecycleError, match="termination"):
        await OpenCollabRuntime().run_workflow(
            _request(
                tmp_path,
                artifact_dir=None,
                environment=environment,
                budget=RunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.01),
            )
        )
    assert time.monotonic() - started < 0.5
    assert environment.revoked
    release.set()
    await asyncio.sleep(0)


class ReplyLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        return LLMResponse(
            content="finished",
            usage=Usage(input_tokens=4, output_tokens=2),
            finish_reason="stop",
        )


class BlockingLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        await asyncio.Event().wait()


async def test_run_agent_returns_stable_metrics_and_persists_final_state(tmp_path: Path) -> None:
    request = AgentRunRequest(
        prompt="finish once",
        config=RuntimeConfig(model="model", provider="provider"),
        llm=ReplyLLM(),
        environment_workdir=str(tmp_path),
        artifact_dir=tmp_path / "agent-run",
        trace=False,
    )

    result = await OpenCollabRuntime().run_agent(request)

    assert result.output == "finished"
    assert result.outcome == "completed"
    assert result.phase == "done"
    assert result.terminal_reason == "completed"
    assert result.error_type is None
    assert result.error_message is None
    assert result.tokens_spent == 6
    assert result.step_count == 1
    assert result.cleanup_quiesced
    assert result.transcript_path is not None and result.transcript_path.is_file()


async def test_run_agent_reports_timeout_after_successful_revocation() -> None:
    environment = PureEnvironment()
    request = AgentRunRequest(
        prompt="block",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.05),
        environment=environment,
        artifact_dir=None,
        trace=False,
        llm=BlockingLLM(),
    )

    with pytest.raises(AgentRunTimeoutError, match="exceeded"):
        await OpenCollabRuntime().run_agent(request)
    assert environment.revoked
    assert environment.abort_calls >= 1


async def test_run_agent_can_return_quiescent_timeout_metrics(tmp_path: Path) -> None:
    environment = PureEnvironment()
    request = AgentRunRequest(
        prompt="block",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.05),
        environment=environment,
        artifact_dir=tmp_path / "timeout-run",
        trace=False,
        failure_mode="return",
        llm=BlockingLLM(),
    )

    result = await OpenCollabRuntime().run_agent(request)

    assert result.outcome == "timed_out"
    assert result.output is None
    assert result.error_type == "AgentRunTimeoutError"
    assert "exceeded" in (result.error_message or "")
    assert result.cleanup_quiesced
    assert result.step_count >= 1
    assert result.transcript_path is not None and result.transcript_path.is_file()
    assert environment.revoked


async def test_run_agent_return_mode_still_rejects_failed_timeout_abort() -> None:
    environment = PureEnvironment(abort_fails=True)
    request = AgentRunRequest(
        prompt="block",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.05),
        environment=environment,
        artifact_dir=None,
        trace=False,
        failure_mode="return",
        llm=BlockingLLM(),
    )

    with pytest.raises(AgentRunLifecycleError, match="quiescent"):
        await OpenCollabRuntime().run_agent(request)


async def test_run_agent_timeout_still_terminates_owner_when_environment_revoke_fails() -> None:
    environment = PureEnvironment(revoke_fails=True)
    cancelled = asyncio.Event()

    class ObservedBlockingLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    request = AgentRunRequest(
        prompt="block",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.05),
        environment=environment,
        artifact_dir=None,
        trace=False,
        failure_mode="return",
        llm=ObservedBlockingLLM(),
    )

    with pytest.raises(AgentRunLifecycleError, match="quiescent"):
        await OpenCollabRuntime().run_agent(request)
    assert environment.abort_calls >= 1
    assert cancelled.is_set()


async def test_run_agent_cancellation_still_terminates_owner_when_environment_revoke_fails() -> None:
    environment = PureEnvironment(revoke_fails=True)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class ObservedBlockingLLM:
        async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    request = AgentRunRequest(
        prompt="block",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=None, cleanup_timeout_seconds=0.05),
        environment=environment,
        artifact_dir=None,
        trace=False,
        failure_mode="return",
        llm=ObservedBlockingLLM(),
    )
    owner = asyncio.create_task(OpenCollabRuntime().run_agent(request))
    await asyncio.wait_for(started.wait(), timeout=1)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert environment.abort_calls >= 1
    assert cancelled.is_set()


async def test_run_agent_timeout_covers_message_delivery_that_swallows_cancellation(
    monkeypatch, tmp_path: Path
) -> None:
    release = asyncio.Event()
    environment = PureEnvironment()

    class ResistantSession:
        auto_save_path = None
        persistence_errors = ()
        pending_cleanup_tasks = ()

        async def add_user_message(self, prompt):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def run_loop(self):
            raise AssertionError("run_loop must not start")

        def enqueue_auto_save(self):
            return None

    monkeypatch.setattr(sdk_runtime, "build_session", lambda **kwargs: ResistantSession())
    request = AgentRunRequest(
        prompt="blocked",
        config=RuntimeConfig(model="model", provider="provider"),
        budget=AgentRunBudget(timeout_seconds=0.01, cleanup_timeout_seconds=0.01),
        environment=environment,
        artifact_dir=None,
        trace=False,
        llm=ReplyLLM(),
    )

    started = time.monotonic()
    with pytest.raises(AgentRunLifecycleError, match="terminal"):
        await OpenCollabRuntime().run_agent(request)
    assert time.monotonic() - started < 0.5
    assert environment.revoked
    release.set()
    await asyncio.sleep(0)


async def test_run_agent_rejects_missing_final_persistence_owner(monkeypatch) -> None:
    environment = PureEnvironment()

    class MissingPersistenceSession:
        auto_save_path = "/tmp/agent.json"
        persistence_errors = ()
        pending_cleanup_tasks = ()
        phase = SimpleNamespace(value="done")
        state = SimpleNamespace(terminal_reason="completed")
        used_tokens = 1
        step_count = 1

        async def add_user_message(self, prompt):
            return None

        async def run_loop(self):
            return "done"

        def enqueue_auto_save(self):
            return None

    monkeypatch.setattr(
        sdk_runtime,
        "build_session",
        lambda **kwargs: MissingPersistenceSession(),
    )
    request = AgentRunRequest(
        prompt="finish",
        config=RuntimeConfig(model="model", provider="provider"),
        environment=environment,
        artifact_dir=None,
        trace=False,
        failure_mode="return",
        llm=ReplyLLM(),
    )

    with pytest.raises(AgentRunLifecycleError, match="persistence"):
        await OpenCollabRuntime().run_agent(request)


async def test_run_agent_can_return_quiescent_execution_failure(monkeypatch) -> None:
    environment = PureEnvironment()

    class FailingSession:
        auto_save_path = None
        persistence_errors = ()
        pending_cleanup_tasks = ()
        phase = SimpleNamespace(value="error")
        state = SimpleNamespace(terminal_reason="provider failed")
        used_tokens = 7
        step_count = 2

        async def add_user_message(self, prompt):
            return None

        async def run_loop(self):
            raise RuntimeError("provider failed")

        def enqueue_auto_save(self):
            return None

    monkeypatch.setattr(sdk_runtime, "build_session", lambda **kwargs: FailingSession())
    request = AgentRunRequest(
        prompt="fail",
        config=RuntimeConfig(model="model", provider="provider"),
        environment=environment,
        artifact_dir=None,
        trace=False,
        failure_mode="return",
        llm=ReplyLLM(),
    )

    result = await OpenCollabRuntime().run_agent(request)

    assert result.outcome == "failed"
    assert result.output is None
    assert result.phase == "error"
    assert result.terminal_reason == "provider failed"
    assert result.error_type == "RuntimeError"
    assert result.error_message == "provider failed"
    assert result.tokens_spent == 7
    assert result.step_count == 2
    assert result.cleanup_quiesced


async def test_run_agent_return_mode_rejects_incomplete_trace_evidence(monkeypatch, tmp_path: Path) -> None:
    class BrokenTracer:
        write_error = "disk write failed"
        dropped_steps = 1

        def __init__(self, run_id, output_dir, filename):
            self.path = str(Path(output_dir) / filename)

        def log_step(self, *args, **kwargs):
            return None

        def flush(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(sdk_runtime, "Tracer", BrokenTracer)
    request = AgentRunRequest(
        prompt="finish once",
        config=RuntimeConfig(model="model", provider="provider"),
        llm=ReplyLLM(),
        environment_workdir=str(tmp_path),
        artifact_dir=tmp_path / "broken-trace-run",
        trace=True,
        failure_mode="return",
    )

    with pytest.raises(AgentRunLifecycleError, match="trajectory"):
        await OpenCollabRuntime().run_agent(request)
