from __future__ import annotations

import json
import subprocess

import pytest

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.sdk import OpenCollab

SOURCE = '''from pathlib import Path

from opencollab import workflow

async def write_report(ctx, args):
    Path(ctx.workspace_root, "report.txt").write_text("prepared report\\n")
    first = await ctx.agent("Prepare report summary.", tools=[])
    final = await ctx.agent("Write final title.", tools=[], over_budget_ok=args["one_more"])
    return {"summary": first, "title": final, "budget": ctx.budget.total}

@workflow(name="local-report")
async def main(ctx, args):
    if not args.get("candidate"):
        return await write_report(ctx, args)
    candidate = await ctx.candidate_workflow(write_report, args, label="report", budget=1000)
    await ctx.adopt_candidate(candidate)
    return {"candidate_output": candidate.output}
'''


@pytest.mark.parametrize(
    "scene", ["ordinary", "escape", "candidate", "inherited", "default-team-budget", "top-level", "unbounded"],
)
async def test_registered_team_workflow_budget_and_top_level_escape(tmp_path, monkeypatch, scene):
    workspace = tmp_path / "workspace"
    workflows = workspace / "workflows"
    workflows.mkdir(parents=True)
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    (workflows / "local_report.py").write_text(SOURCE)
    if scene == "candidate":
        for command in (
            ("init", "-q"), ("add", "workflows/local_report.py"),
            ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "Prepare workflow fixture"),
        ):
            subprocess.run(["git", *command], cwd=workspace, check=True, capture_output=True)
    config = tmp_path / "team.yaml"
    config.write_text(
        "entry: writer\nroles:\n  writer:\n    prompt: Writer\n"
        "    tools: [run_workflow]\n"
    )
    seen = {}
    calls = []
    inputs = {"one_more": scene != "ordinary", "candidate": scene == "candidate"}
    direct = scene in {"top-level", "unbounded"}

    class Model:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 200_000

        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return 10

        async def close(self):
            pass

        async def complete(self, messages, **kwargs):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if prompt == "Prepare report summary.":
                calls.append("summary")
                return LLMResponse(content="Report summary", usage=Usage(978, 22), finish_reason="stop")
            if prompt == "Write final title.":
                calls.append("title")
                return LLMResponse(content="Final title", usage=Usage(40, 10), finish_reason="stop")
            replies = [message for message in messages if message["role"] == "tool"]
            if not replies:
                arguments = {"name": "local-report", "args": inputs}
                if scene not in {"inherited", "default-team-budget"}:
                    arguments["budget"] = 1000
                call = {
                    "id": "local-report", "type": "function", "function": {
                        "name": "run_workflow", "arguments": json.dumps(arguments),
                    },
                }
                return LLMResponse(tool_calls=[call], finish_reason="tool_calls", usage=Usage(3, 2))
            seen.update(json.loads(replies[-1]["content"]))
            return LLMResponse(content="Report reviewed.", usage=Usage(3, 2), finish_reason="stop")

    if scene == "unbounded":
        monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    client = OpenCollab(workspace, model="offline-model", config={"max_output_tokens": 128})
    if direct:
        result = await client.workflow(
            "local-report", inputs, llm=Model(), budget=None if scene == "unbounded" else 1000,
            limit_mode="environment" if scene == "unbounded" else "explicit",
            trace=False, artifacts=tmp_path / "artifacts", timeout=12,
        )
        seen.update(output=result.output, stop_reason=None, tokens=result.tokens)
    else:
        monkeypatch.setattr(container, "LLMClient", Model)
        result = await client.team(
            "Complete a local report.", config=config, budget=None if scene == "default-team-budget" else 6000,
            trace=False, artifacts=tmp_path / "artifacts", use_worktrees=False, timeout=12,
        )
    assert result.ok, (result.status, result.reason, result.error)
    assert (workspace / "report.txt").read_text() == "prepared report\n"
    allowed = direct or scene in {"inherited", "default-team-budget"}
    assert calls == (["summary", "title"] if allowed else ["summary"])
    expected_child_tokens = 1050 if allowed else 1000
    assert seen["tokens"] == expected_child_tokens
    assert result.tokens == expected_child_tokens + (0 if direct else 10)
    if scene == "candidate":
        assert seen["output"] == {"candidate_output": None}
        assert seen["agent_failures"][0]["exception_type"] == "WorkflowBudgetExceeded"
    elif allowed:
        assert seen["output"]["title"] == "Final title"
        assert seen["stop_reason"] is None
        if scene in {"inherited", "default-team-budget"}:
            assert seen["output"]["budget"] > seen["tokens"]
        elif scene == "unbounded":
            assert seen["output"]["budget"] is None
    else:
        assert seen["stop_reason"] == "budget_exceeded"
