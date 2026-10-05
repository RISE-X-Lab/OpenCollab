"""Every SDK run has one run id, and every file the run writes carries it.

A single agent used to stamp its trajectory with its name and a workflow with
the workflow's name, so every run of one setting shared one "run id" and the
records of two runs could not be told apart. Only a team generated a unique
one. A harness that launches runs can also pass its own id, so the files it
writes join the files the run writes on one key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from opencollab import OpenCollab, workflow
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import build_runtime_context, build_scheduler
from tests.support.prebuilt_team_test_support import CONFIG


class ReplyLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        return LLMResponse(
            content="finished",
            usage=Usage(input_tokens=4, output_tokens=2),
            finish_reason="stop",
        )


@workflow(name="echo-flow", description="Echo one value")
async def echo(_ctx, inputs):
    return {"answer": inputs["value"] + 1}


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _agent(tmp_path: Path, artifacts: Path, *, trace: bool = True, **kwargs):
    return await OpenCollab(tmp_path, model="model", provider="openai").agent(
        "finish once",
        tools=(),
        llm=ReplyLLM(),
        artifacts=artifacts,
        trace=trace,
        **kwargs,
    )


class FailingLLM:
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        raise RuntimeError("controlled agent failure")


async def test_an_agent_run_id_is_unique_and_joins_trajectory_and_result(tmp_path):
    first = await _agent(tmp_path, tmp_path / "a")
    second = await _agent(tmp_path, tmp_path / "b")

    run_id = first.metrics["run_id"]
    assert re.fullmatch(r"agent-[0-9a-f]{32}", run_id)
    assert second.metrics["run_id"] != run_id
    records = _records(tmp_path / "a" / "trajectory.jsonl")
    assert records
    assert {record["run_id"] for record in records} == {run_id}


async def test_an_agent_uses_the_run_id_its_caller_supplies(tmp_path):
    result = await _agent(tmp_path, tmp_path / "a", run_id="batch-7-single")

    assert result.metrics["run_id"] == "batch-7-single"
    records = _records(tmp_path / "a" / "trajectory.jsonl")
    assert {record["run_id"] for record in records} == {"batch-7-single"}


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("run_id", [None, "batch-7-single"])
@pytest.mark.parametrize("fails", [False, True])
async def test_agent_snapshot_carries_run_identity_independently_of_tracing(
    tmp_path, trace, run_id, fails
):
    artifacts = tmp_path / "artifacts"
    result = await OpenCollab(tmp_path, model="model", provider="openai").agent(
        "finish once",
        tools=(),
        llm=FailingLLM() if fails else ReplyLLM(),
        artifacts=artifacts,
        trace=trace,
        run_id=run_id,
    )

    snapshot = json.loads((artifacts / "agent.json").read_text(encoding="utf-8"))
    canonical_id = result.metrics["run_id"]
    assert snapshot["run_id"] == canonical_id
    assert result.status == ("failed" if fails else "completed")
    if run_id is not None:
        assert canonical_id == run_id
    else:
        assert re.fullmatch(r"agent-[0-9a-f]{32}", canonical_id)
    if trace:
        assert {row["run_id"] for row in _records(artifacts / "trajectory.jsonl")} == {
            canonical_id
        }


async def test_a_workflow_run_id_is_unique_and_joins_manifest_and_result(tmp_path):
    client = OpenCollab(tmp_path)
    first = await client.workflow(echo, {"value": 3}, artifacts=tmp_path / "a", trace=True)
    second = await client.workflow(echo, {"value": 3}, artifacts=tmp_path / "b", trace=True)

    run_id = first.metrics["run_id"]
    assert re.fullmatch(r"workflow-[0-9a-f]{32}", run_id)
    assert second.metrics["run_id"] != run_id
    manifest = json.loads((tmp_path / "a" / "workflow.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == run_id


async def test_a_workflow_uses_the_run_id_its_caller_supplies(tmp_path):
    result = await OpenCollab(tmp_path).workflow(
        echo, {"value": 3}, artifacts=tmp_path / "a", trace=True, run_id="batch-7-duo"
    )

    assert result.metrics["run_id"] == "batch-7-duo"
    manifest = json.loads((tmp_path / "a" / "workflow.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "batch-7-duo"


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("run_id", [None, "batch-7-workflow"])
@pytest.mark.parametrize("fails", [False, True])
async def test_workflow_manifest_keeps_identity_independently_of_tracing(tmp_path, trace, run_id, fails):
    async def run(ctx, args):
        await ctx.log("workflow entered")
        if fails:
            raise ValueError("controlled workflow failure")
        return "done"

    artifacts = tmp_path / "run"
    result = await OpenCollab(tmp_path).workflow(run, artifacts=artifacts, trace=trace, run_id=run_id)
    manifest = json.loads((artifacts / "workflow.json").read_text())

    assert result.status == ("failed" if fails else "completed")
    assert manifest["run_id"] == result.metrics["run_id"]
    if run_id is None:
        assert re.fullmatch(r"workflow-[0-9a-f]{32}", manifest["run_id"])
    else:
        assert manifest["run_id"] == run_id
    assert manifest["trace_enabled"] is trace


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("explicit_run_id", [None, "harness-run-9"])
async def test_scheduler_manifest_uses_one_run_identity(tmp_path, trace, explicit_run_id):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = build_runtime_context(
        str(workspace), dict(CONFIG), trace=trace, run_id_prefix="scheduler-"
    )
    scheduler = build_scheduler(
        ctx,
        use_worktrees=False,
        interactive=False,
        auto_save=True,
        run_id=explicit_run_id,
    )
    try:
        scheduler._manifest_writer()
        run_dir = Path(scheduler.lead_session.auto_save_path).parent
        manifest = json.loads((run_dir / "team.json").read_text(encoding="utf-8"))

        if explicit_run_id is not None:
            expected = explicit_run_id
        elif ctx.tracer is not None:
            expected = ctx.tracer.run_id
        else:
            expected = run_dir.name
        assert manifest["run_id"] == expected
    finally:
        if ctx.tracer is not None:
            ctx.tracer.close()
        await scheduler.cleanup()


@pytest.mark.parametrize("bad", ["", "  ", 7])
async def test_a_run_id_must_be_a_non_empty_string(tmp_path, bad):
    client = OpenCollab(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        await client.agent("run", run_id=bad)
    with pytest.raises(ValueError, match="run_id"):
        await client.workflow(echo, {"value": 3}, run_id=bad)
    with pytest.raises(ValueError, match="run_id"):
        await client.team("run", run_id=bad)
