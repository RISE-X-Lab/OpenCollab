from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from opencollab.sdk import (
    OpenCollabRuntime,
    RunBudget,
    RuntimeConfig,
    WorkflowManifestError,
    WorkflowRunRequest,
    WorkflowRunTimeoutError,
    workflow,
)
from opencollab.sdk import runtime as sdk_runtime


@workflow(name="sample", description="sample workflow")
async def sample_workflow(ctx, args):
    return args


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
