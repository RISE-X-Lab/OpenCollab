"""SDK artifacts share a run ID that distinguishes repeated runs of the same setting."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from opencollab import OpenCollab, RunControl, workflow
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import build_runtime_context, build_scheduler, build_session, load_session
from opencollab.domain.agent import Agent
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
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

@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize(
    ("load_kwargs", "expected_run_id"),
    [({}, "resume-id"), ({"run_id": "continued-id"}, "continued-id"), ({"run_id": None}, None)],
)
async def test_loading_an_sdk_snapshot_keeps_the_selected_run_identity(local_environment_factory,
    tmp_path, trace, load_kwargs, expected_run_id
):
    artifacts = tmp_path / "original"
    first = await _agent(
        tmp_path, artifacts, trace=trace, run_id="resume-id",
        name="saved-agent", system_prompt="Answer briefly.",
    )
    source = artifacts / "agent.json"
    original = OpenCollab.read_session_snapshot(source)
    assert first.metrics["run_id"] == original["run_id"] == "resume-id"
    events = []
    restored = load_session(
        str(source),
        env=local_environment_factory(), agent=Agent(name="saved-agent", system_prompt="Answer briefly."),
        llm=ReplyLLM(),
        run_control=RunControl(on_event=events.append),
        **load_kwargs,
    )
    try:
        await restored.add_user_message("continue")
        assert await restored.run_loop() == "finished"
        saved_path = tmp_path / "continued.json"
        restored.save(str(saved_path))
        continued = OpenCollab.read_session_snapshot(saved_path)

        assert restored.run_id == expected_run_id
        assert [event.run_id for event in events if event.type == "usage"] == [expected_run_id]
        assert continued.get("run_id") == expected_run_id
        assert continued["aid"] == original["aid"]
        assert continued["session_state"]["used_tokens"] == 12
        answers = [
            message["content"] for message in continued["messages"]
            if message["role"] == "assistant"
        ]
        assert answers == ["finished", "finished"]
    finally:
        await restored.aclose()

@pytest.mark.parametrize("autosave", [False, True])
async def test_replacing_an_export_during_resume_keeps_its_run_identity(
    tmp_path, local_environment_factory, monkeypatch, autosave,
):
    env = local_environment_factory(str(tmp_path))
    agent = Agent(name="saved-agent", system_prompt="Answer briefly.")
    source = str(tmp_path / "shared.json")
    target = str(tmp_path / "continued.json")
    store = SessionStore()
    load_snapshot = store.load_snapshot
    read_first = threading.Event()
    replaced = threading.Event()
    events = []

    def read_with_concurrent_export(path, system_prompt):
        snapshot = load_snapshot(path, system_prompt)
        if path == source and not read_first.is_set():
            read_first.set()
            assert replaced.wait(5)
        return snapshot

    monkeypatch.setattr(store, "load_snapshot", read_with_concurrent_export)
    async with AsyncExitStack() as sessions:
        original, replacement = (
            build_session(agent=agent, env=env, llm=ReplyLLM(), aid=aid, run_id=run_id)
            for aid, run_id in ((3, "review-a"), (7, "review-b"))
        )
        for writer, prompt in ((original, "Review A."), (replacement, "Review B.")):
            sessions.push_async_callback(writer.aclose)
            await writer.add_user_message(prompt)
            assert await writer.run_loop() == "finished"
        original.save(source)

        reader = asyncio.create_task(asyncio.to_thread(
            load_session, source, agent, env=env, llm=ReplyLLM(), store=store,
            auto_save_path=target if autosave else None,
            run_control=RunControl(on_event=events.append),
        ))
        try:
            assert await asyncio.to_thread(read_first.wait, 5)
            replacement.save(source)
            assert OpenCollab.read_session_snapshot(source)["run_id"] == "review-b"
        finally:
            replaced.set()
            restored = await reader
            sessions.push_async_callback(restored.aclose)
        assert restored.state.aid == 3
        assert restored.run_id == "review-a"
        assert restored.messages == original.messages
        await restored.add_user_message("Summarize the review.")
        assert await restored.run_loop() == "finished"
        if not autosave:
            restored.save(target)
        continued = OpenCollab.read_session_snapshot(target)
        assert continued["run_id"] == "review-a"
        assert continued["aid"] == 3
        assert continued["session_state"]["used_tokens"] == 12
        assert [event.run_id for event in events if event.type == "usage"] == ["review-a"]
        assert OpenCollab.read_session_snapshot(source)["run_id"] == "review-b"

@pytest.mark.parametrize("saved_id", [None, "", "  ", 7, "invalid\x00id"])
def test_loading_an_unusable_saved_run_id_preserves_agent_identity(local_environment_factory, tmp_path, saved_id):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({
        "messages": [{"role": "system", "content": "sys"}],
        "aid": 7,
        "run_id": saved_id,
    }), encoding="utf-8")

    restored = load_session(str(path), env=local_environment_factory(),
    agent=Agent(name="saved", system_prompt="sys"), llm=ReplyLLM())

    assert restored.run_id is None
    assert restored.state.aid == 7

@pytest.mark.parametrize("document", [None, "{}"])
def test_loading_an_unreadable_snapshot_preserves_the_restore_error(local_environment_factory, tmp_path, document):
    path = tmp_path / "snapshot.json"
    if document is not None:
        path.write_text(document, encoding="utf-8")

    error = FileNotFoundError if document is None else ValueError
    message = str(path) if document is None else "missing required 'messages' list"
    with pytest.raises(error, match=re.escape(message)):
        load_session(str(path), env=local_environment_factory(),
        agent=Agent(name="saved", system_prompt="sys"), llm=ReplyLLM())

async def test_a_workflow_run_id_is_unique_and_joins_manifest_and_result(tmp_path):
    client = OpenCollab(tmp_path)
    first = await client.workflow(echo, {"value": 3}, artifacts=tmp_path / "a", trace=True)
    second = await client.workflow(echo, {"value": 3}, artifacts=tmp_path / "b", trace=True)

    run_id = first.metrics["run_id"]
    assert re.fullmatch(r"workflow-[0-9a-f]{32}", run_id)
    assert second.metrics["run_id"] != run_id
    manifest = json.loads((tmp_path / "a" / "workflow.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == run_id

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
