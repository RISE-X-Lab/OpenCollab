"""Public candidate operations honor their shared source environment state."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow import WorkflowEnvironmentRevoked
from opencollab.tools import builtin_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete_cleanup", [False, True])
async def test_candidate_creation_and_adoption_share_source_availability(tmp_path, incomplete_cleanup):
    marker = tmp_path / "marker.txt"
    marker.write_text("before")
    for args in (("init", "-q"), ("add", "marker.txt"),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "-qm", "initial")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    writing_started, release_writer = asyncio.Event(), asyncio.Event()
    independent_started, release_independent = asyncio.Event(), asyncio.Event()

    class LateWrite:
        name, description = "late_write", "write during controlled cancellation cleanup"
        parameters = {"type": "object", "properties": {}}

        def to_openai_schema(self):
            return {"type": "function", "function": {
                "name": self.name, "description": self.description, "parameters": self.parameters,
            }}

        async def execute_with_runtime(self, params, runtime):
            writing_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_writer.wait()
                marker.write_text("after")
                return "written"

    class LocalLLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools=None, **kwargs):
            self.calls += 1
            prompt = str(messages[-1]["content"])
            if "independent" in prompt:
                independent_started.set()
                await release_independent.wait()
            if messages[-1]["role"] != "tool" and "make-candidate" in prompt:
                return LLMResponse(tool_calls=[{
                    "id": "candidate-write", "type": "function", "function": {
                        "name": "file_write", "arguments": json.dumps({
                            "path": "marker.txt", "mode": "create", "content": "candidate",
                        }),
                    },
                }], usage=Usage(4, 2), finish_reason="tool_calls")
            if "slow" in prompt:
                if incomplete_cleanup:
                    return LLMResponse(tool_calls=[{
                        "id": "late-write", "type": "function", "function": {
                            "name": "late_write", "arguments": "{}",
                        },
                    }], usage=Usage(4, 2), finish_reason="tool_calls")
                writing_started.set()
                await release_writer.wait()
            return LLMResponse(content="finished", usage=Usage(4, 2), finish_reason="stop")

    llm = LocalLLM()

    async def flow(ctx, _args):
        async def edit(child, _inputs):
            return await child.agent_run(
                "make-candidate", tools=list(builtin_tools("file_write", headless=False)),
                budget=2_000, system_prompt="brief",
            )

        async def finish(child, _inputs):
            return await child.agent_run(
                _inputs.get("prompt", "finish"), tools=[], budget=1_000, system_prompt="brief",
            )

        candidate = await ctx.candidate_workflow(edit, {}, label="original", budget=2_000)
        original_diff = candidate.diff
        assert original_diff and candidate.output.status == "completed"
        assert marker.read_text() == "before"
        independent = asyncio.create_task(ctx.candidate_workflow(
            finish, {"prompt": "independent"}, label="already-running", budget=1_000,
        ))
        await asyncio.wait_for(independent_started.wait(), 2)
        writing = asyncio.create_task(ctx.agent_run(
            "slow", tools=[LateWrite()] if incomplete_cleanup else [], budget=2_000,
            system_prompt="brief", timeout=.1 if incomplete_cleanup else None, cleanup_timeout=.02,
            run_control=RunControl(tool_cancellation_cleanup_timeout=10),
        ))
        try:
            await asyncio.wait_for(writing_started.wait(), 2)
            if not incomplete_cleanup:
                release_writer.set()
            row = await writing
            release_independent.set()
            continuing = await independent
            assert continuing.output.status == "completed" and continuing.output.workspace_ready
            if incomplete_cleanup:
                assert row.reason == "cleanup incomplete"
                calls_before = llm.calls
                with pytest.raises(WorkflowEnvironmentRevoked):
                    await ctx.candidate_agent("finish", label="extra-agent", tools=[], budget=1_000)
                with pytest.raises(WorkflowEnvironmentRevoked):
                    await ctx.candidate_workflow(finish, {}, label="extra-flow", budget=1_000)
                with pytest.raises(WorkflowEnvironmentRevoked):
                    await ctx.adopt_candidate(candidate)
                assert llm.calls == calls_before
                assert marker.read_text() == "before"
                assert ctx.pending_cleanup_tasks
            else:
                assert row.status == "completed" and row.workspace_ready
                extra_agent = await ctx.candidate_agent(
                    "finish", label="extra-agent", tools=[], budget=2_000,
                )
                extra_flow = await ctx.candidate_workflow(finish, {}, label="extra-flow", budget=1_000)
                assert extra_agent.output == "finished"
                assert extra_flow.output.status == "completed"
                await ctx.adopt_candidate(candidate)
                assert marker.read_text() == "candidate"
            assert candidate.diff == original_diff
            assert candidate.output.status == "completed"
            return candidate
        finally:
            release_writer.set()
            release_independent.set()
            await asyncio.gather(writing, independent, return_exceptions=True)
            await ctx.wait_for_pending_cleanup()

    result = await OpenCollab(tmp_path, model="test-model", config={"max_output_tokens": 128}).workflow(
        flow, llm=llm, budget=20_000, concurrency=2, limit_mode="explicit", agent_profile="single2",
    )
    assert result.ok
    assert marker.read_text() == ("after" if incomplete_cleanup else "candidate")
    assert result.output.diff and result.output.output.status == "completed"
