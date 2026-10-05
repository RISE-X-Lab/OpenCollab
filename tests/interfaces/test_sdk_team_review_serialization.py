"""Public teams deliver reviewed files while serializing agent execution."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.scheduler import Scheduler
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.bootstrap import container


def _call(name, arguments, step):
    return {"id": f"call-{step}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


@pytest.fixture
def reviewed_team(tmp_path, monkeypatch):
    observed = {
        "calls": [], "closed": [], "review_failures": 0, "ordinary": False,
        "active": [], "work": [], "serialized": True,
    }
    config = tmp_path / "team.yaml"
    config.write_text(
        """entry: lead
roles:
  lead:
    prompt: ROLE_LEAD
    tools: [spawn_agent, spawn_with_review, file_write]
  coder:
    prompt: ROLE_CODER
    tools: [file_write]
  reviewer:
    prompt: ROLE_REVIEWER
    tools: [file_read]
topology:
  lead: [coder, reviewer]
""", encoding="utf-8",
    )

    class ControlledModel:
        def __init__(self, **kwargs):
            self.step = 0
            self.role = None

        def context_window(self):
            return 200_000

        async def close(self):
            observed["closed"].append(self.role)

        async def complete(self, messages, **kwargs):
            system = str(messages[0].get("content", ""))
            role = next(
                role for role in ("lead", "coder", "reviewer")
                if f"ROLE_{role.upper()}" in system
            )
            if observed["serialized"]:
                assert not observed["active"]
            observed["active"].append(role)
            observed["work"].append((role, "model"))
            try:
                await asyncio.sleep(0)
                return await self._complete(messages, **kwargs)
            finally:
                observed["active"].remove(role)

        async def _complete(self, messages, **kwargs):
            self.step += 1
            system = str(messages[0].get("content", ""))
            self.role = next(
                role for role in ("lead", "coder", "reviewer")
                if f"ROLE_{role.upper()}" in system
            )
            observed["calls"].append((self.role, self.step))
            if self.role == "coder" and observed.get("block_coder"):
                observed["coder_entered"].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    observed["coder_cancelled"] = True
            calls = []
            if self.step == 1:
                if self.role == "lead":
                    name = "spawn_agent" if observed["ordinary"] else "spawn_with_review"
                    arguments = {"task": "Create answer.py with answer = 42."}
                    arguments.update(
                        {"role": "coder"} if observed["ordinary"]
                        else {"max_iterations": observed.get("iterations", 1)}
                    )
                elif self.role == "coder":
                    name = "file_write"
                    arguments = {"path": "answer.py", "mode": "create", "content": "answer = 42\n"}
                else:
                    name = "file_read"
                    arguments = {"path": "answer.py"}
                calls = [_call(name, arguments, self.step)]
            elif self.role == "lead" and self.step == 2 and observed.get("lead_edit"):
                calls = [_call("file_write", {
                    "path": "lead.txt", "mode": "create", "content": "lead result\n",
                }, self.step)]
            if self.role == "lead":
                content = "DELIVERED " + "\n".join(
                    str(message.get("content", ""))
                    for message in messages if message.get("role") == "tool"
                )
            elif self.role == "coder":
                content = "Created answer.py with answer = 42."
            else:
                assert any(
                    "answer = 42" in str(message.get("content", ""))
                    for message in messages if message.get("role") == "tool"
                ) or calls
                failed = observed["review_failures"] > 0
                if failed and not calls:
                    observed["review_failures"] -= 1
                content = "Check the implementation.\nVERDICT: " + ("FAIL" if failed else "PASS")
            return LLMResponse(
                content=None if calls else content,
                tool_calls=calls,
                usage=Usage(input_tokens=10, output_tokens=5),
                finish_reason="tool_calls" if calls else "stop",
            )

    schedulers = []
    original_init = Scheduler.__init__

    def capture_scheduler(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        schedulers.append(self)

    monkeypatch.setattr(Scheduler, "__init__", capture_scheduler)
    monkeypatch.setattr(container, "LLMClient", ControlledModel)
    execute_tool = ToolExecutionUseCase.execute_tool

    async def capture_tool_work(self, tool, args, **kwargs):
        if tool.name == "spawn_with_review":
            return await execute_tool(self, tool, args, **kwargs)
        role = self.agent.name
        if observed["serialized"]:
            assert not observed["active"]
        observed["active"].append(role)
        observed["work"].append((role, tool.name))
        try:
            return await execute_tool(self, tool, args, **kwargs)
        finally:
            observed["active"].remove(role)

    monkeypatch.setattr(ToolExecutionUseCase, "execute_tool", capture_tool_work)
    client = OpenCollab(
        tmp_path, model="controlled", provider="openai",
        api_key="local-fixture",  # pragma: allowlist secret
    )

    async def run(*, serialized=True, timeout=5, record_delivery_tree=False):
        observed["serialized"] = serialized
        return await client.team(
            "Create answer.py with answer = 42 and review it.",
            config=config, use_worktrees=False, serialize_turns=serialized,
            timeout=timeout, cleanup_timeout=2, trace=False, budget=100_000,
            record_delivery_tree=record_delivery_tree,
        )

    return run, observed, schedulers


@pytest.mark.parametrize("serialized,ordinary", [(True, False), (False, False), (True, True)])
async def test_public_team_runs_coder_and_reviewer_and_delivers_file(
    tmp_path, reviewed_team, serialized, ordinary,
):
    run, observed, _schedulers = reviewed_team
    observed["ordinary"] = ordinary
    result = await run(serialized=serialized)
    assert result.status == "completed", result.reason
    assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
    roles = [role for role, _step in observed["calls"]]
    assert roles == (["lead", "coder", "coder", "lead"] if ordinary else [
        "lead", "coder", "coder", "reviewer", "reviewer", "lead",
    ])
    if not ordinary:
        assert "PASSED after 1 iteration" in result.output
        assert ("reviewer", "file_read") in observed["work"]
    assert ("coder", "file_write") in observed["work"]


async def test_resumed_parent_edit_has_its_own_delivery_tree_boundary(tmp_path, reviewed_team):
    for args in (
        ("init", "-q"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid",
         "commit", "--allow-empty", "-qm", "\u6d4b\u8bd5\u521d\u59cb\u76ee\u5f55"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    run, observed, schedulers = reviewed_team
    observed["lead_edit"] = True
    result = await run(record_delivery_tree=True)
    assert result.status == "completed", result.reason
    assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
    assert (tmp_path / "lead.txt").read_text() == "lead result\n"
    snapshots = result.metrics["tree_snapshots"]
    assert [(row["at"], row["role"]) for row in snapshots] == [
        ("turn_start", "lead"), ("turn_start", "coder"),
        ("turn_start", "reviewer"), ("turn_start", "lead"),
    ]

    def diff(row):
        return row.get("diff") or snapshots[row["unchanged_since"]]["diff"]

    assert "answer = 42" in diff(snapshots[2])
    assert "lead result" not in diff(snapshots[3])
    assert "lead result" in await schedulers[-1]._delivery_tree_probe.diff()


@pytest.mark.parametrize("failures,expected", [(1, "PASSED"), (2, "FAILED")])
async def test_serial_review_respects_iteration_limit_and_preserves_artifact(
    tmp_path, reviewed_team, failures, expected,
):
    run, observed, _schedulers = reviewed_team
    observed.update(review_failures=failures, iterations=2)
    result = await run()
    assert result.status == "completed", result.reason
    assert expected + " after 2 iteration(s)" in result.output
    assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
    roles = [role for role, step in observed["calls"] if step == 1]
    assert roles == ["lead", "coder", "reviewer", "coder", "reviewer"]


@pytest.mark.parametrize("serialized", [False, True])
async def test_review_wait_timeout_cleans_resources_and_later_public_run_completes(
    tmp_path, reviewed_team, serialized,
):
    run, observed, schedulers = reviewed_team
    observed.update(block_coder=True, coder_entered=asyncio.Event())
    result = await run(serialized=serialized, timeout=1.0)
    assert observed["coder_entered"].is_set()
    assert result.status == "stopped"
    assert result.reason == "timeout"
    assert observed["coder_cancelled"]
    scheduler = schedulers[-1]
    assert not scheduler._active_scheduler_tasks()
    assert not scheduler._turn_lease
    assert not scheduler._lease_baseline
    assert scheduler.allocated_tokens == result.tokens == 15
    assert not scheduler._turn_waiters
    assert scheduler._turn_gate_lock is None or not scheduler._turn_gate_lock.locked()
    assert set(observed["closed"]) >= {"lead", "coder"}
    observed.update(block_coder=False, calls=[])
    result = await run(serialized=serialized)
    assert result.status == "completed", result.reason
    assert "PASSED after 1 iteration" in result.output
    assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
