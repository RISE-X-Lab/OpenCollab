"""Integration tests for the thin SDK-to-bootstrap delegation layer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab import OpenCollab, RunError, workflow
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import programmatic
from opencollab.bootstrap.programmatic import (
    ProgrammaticLifecycleError,
    ProgrammaticResult,
)
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
    result = await OpenCollab(
        tmp_path,
        model="model",
        provider="openai",
    ).agent(
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
    assert result.metrics == {"steps": 1}
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
        steps=3,
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
    assert captured["tools"] == "read"
    assert captured["artifacts"] == (tmp_path / "evidence").resolve()
    assert "private-key" not in repr(client)


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
    assert result.metrics == {"sessions": 0}
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["workflow"] == "echo-flow"
    assert manifest["args"] == {"value": 3}


async def test_workflow_execution_failure_is_a_result(tmp_path: Path) -> None:
    @workflow
    async def broken(_ctx, _inputs):
        raise ValueError("bad workflow")

    result = await OpenCollab(tmp_path).workflow(broken, trace=False)
    assert result.status == "failed"
    assert result.reason == "bad workflow"
    assert result.tokens is None
    assert isinstance(result.error, ValueError)
    with pytest.raises(RunError, match="bad workflow"):
        result.raise_for_status()


async def test_workflow_timeout_is_a_stopped_result(tmp_path: Path) -> None:
    @workflow
    async def blocked(_ctx, _inputs):
        await asyncio.Event().wait()

    result = await OpenCollab(tmp_path).workflow(
        blocked,
        timeout=0.01,
        trace=False,
    )
    assert result.status == "stopped"
    assert result.reason == "timeout"
    assert result.tokens is None
    assert isinstance(result.error, TimeoutError)


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


async def test_artifact_directory_must_be_new_or_empty(tmp_path: Path) -> None:
    artifacts = tmp_path / "existing"
    artifacts.mkdir()
    (artifacts / "keep.txt").write_text("user data")

    async def plain(_ctx, _inputs):
        return None

    with pytest.raises(ValueError, match="new or empty"):
        await OpenCollab(tmp_path).workflow(plain, artifacts=artifacts, trace=False)
    assert (artifacts / "keep.txt").read_text() == "user data"


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
        use_worktrees=False,
    )

    assert result.output == "team done"
    assert result.tokens == 11
    assert captured["team_config_path"] == config.resolve()
    assert captured["max_tokens"] == 90
    assert captured["use_worktrees"] is False


async def test_shared_team_runtime_always_cleans_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeScheduler:
        def __init__(self) -> None:
            self.cleaned = False
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
            assert cleanup_timeout > 0
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
        artifacts=None,
        trace=False,
        use_worktrees=False,
    )

    assert scheduler.cleaned
    assert result.status == "completed"
    assert result.tokens == 8
    assert result.metrics == {"steps": 2, "sessions": 1}


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
