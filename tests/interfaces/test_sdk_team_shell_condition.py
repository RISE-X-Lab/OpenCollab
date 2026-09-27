"""The experimental condition, asserted at the SDK entry point.

The Team arm of the handoff experiment is started with ``client.team()``, and
what makes that arm an arm at all is a pair of facts about the run: every
declared role is seated before the first model call, and a seated teammate's
``bash`` actually runs a command. Neither was reachable from the SDK — the team
runtime hardcoded ``interactive=False``, which switched off the shell along with
``ask_user``, and never forwarded ``prebuild_team``.

So this drives the real scheduler, real worktrees, the real ``BashTool`` and
real ``git`` through ``OpenCollab.team`` with one scripted LLM in place of a
provider, and reads the outcome out of the run's own trajectory. The control is
the same run with ``allow_unisolated_shell`` left alone: if the seated Coder can
run ``git`` either way, the switch is not what produced the condition.

Agent 0 is checked for the other half. An SDK run has nobody sitting at it, so
no agent in it may hold ``ask_user`` — the shell must arrive without dragging a
question nobody can answer along with it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.sdk import OpenCollab

ANALYST_AID, CODER_AID = 0, 1


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    """A committed repository — the precondition for linked worktrees."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _team_file(path: Path) -> Path:
    path.write_text(
        """
entry: analyst
roles:
  analyst:
    prompt: You are the Analyst. Hand the work to the coder.
    tools: [ask_user, message_agent]
  coder:
    prompt: You are the Coder. Run what you are asked to run.
    tools: [bash, message_agent]
topology:
  analyst: [coder]
  coder: [analyst]
""".strip(),
        encoding="utf-8",
    )
    return path


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _scripted_llm(observed: dict):
    """One scripted provider class; each agent gets its own instance."""

    class ScriptedLLM:
        def __init__(self, **_kwargs) -> None:
            self.role = "?"
            self.turn = 0

        def context_window(self) -> int:
            return 200_000

        async def close(self) -> None:
            return None

        async def complete(self, messages, **_kwargs) -> LLMResponse:
            system = str(messages[0].get("content") or "")
            for role in ("Analyst", "Coder"):
                if f"You are the {role}" in system:
                    self.role = role.lower()
                    break
            self.turn += 1
            usage = Usage(input_tokens=10, output_tokens=5)
            if self.role == "analyst" and self.turn == 1:
                return LLMResponse(
                    tool_calls=[
                        _call(
                            "a1",
                            "message_agent",
                            {
                                "to_aid": CODER_AID,
                                "summary": "read HEAD",
                                "content": "Run `git rev-parse HEAD` and tell me what it says.",
                            },
                        )
                    ],
                    usage=usage,
                    finish_reason="tool_calls",
                )
            if self.role == "coder" and self.turn == 1:
                return LLMResponse(
                    tool_calls=[_call("c1", "bash", {"command": "git rev-parse HEAD"})],
                    usage=usage,
                    finish_reason="tool_calls",
                )
            if self.role == "coder" and self.turn == 2:
                for message in reversed(messages):
                    if message.get("role") == "tool":
                        observed["coder_bash_result"] = str(message.get("content") or "")
                        break
            return LLMResponse(content="done", usage=usage, finish_reason="stop")

    return ScriptedLLM


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **switches) -> dict:
    workspace = _repo(tmp_path / "repo")
    artifacts = tmp_path / "artifacts"
    observed: dict = {"head": _git(workspace, "rev-parse", "HEAD")}
    monkeypatch.setattr(container, "LLMClient", _scripted_llm(observed))

    client = OpenCollab(
        workspace,
        config={
            "model": "gpt-4o",
            "provider": "openai",
            "api_key": "test-key",  # pragma: allowlist secret
            "base_url": None,
            "budget": 2_000_000,
        },
    )
    observed["result"] = await client.team(
        "Find out what HEAD is.",
        config=_team_file(tmp_path / "team.yaml"),
        artifacts=artifacts,
        trace=True,
        use_worktrees=True,
        prebuild_team=True,
        **switches,
    )
    records = [
        json.loads(line)
        for line in (artifacts / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    nodes = [
        record["payload"]["nodes"]
        for record in records
        if record["type"] == "assigned.topology_nodes"
    ]
    assert len(nodes) == 1, "a prebuilt run records its assigned roster exactly once"
    observed["nodes"] = {node["role"]: node for node in nodes[0]}
    return observed


async def test_sdk_team_seats_a_working_shell_without_a_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = await _run(tmp_path, monkeypatch, allow_unisolated_shell=True)

    # The Coder ran real git in its own worktree and got a real sha back.
    assert "Exit code: 0" in observed["coder_bash_result"]
    assert observed["head"] in observed["coder_bash_result"]

    # And the run says so in its own records, per seat.
    assert observed["nodes"]["coder"]["shell"] == "available"
    assert observed["nodes"]["coder"]["workspace_isolated"] is True

    # The other half: nobody is sitting at an SDK run, so nobody may ask.
    assert observed["nodes"]["analyst"]["entry"] is True
    assert "ask_user" not in observed["nodes"]["analyst"]["tools"]
    assert observed["result"].status == "completed"


async def test_sdk_team_shell_stays_off_unless_the_run_asks_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control. Same entry point, same team, switch left alone: the seated
    # Coder holds bash and every command it sends is refused instead of run.
    observed = await _run(tmp_path, monkeypatch)

    assert "bash is disabled" in observed["coder_bash_result"]
    assert observed["head"] not in observed["coder_bash_result"]
    assert observed["nodes"]["coder"]["shell"] == "sandbox_required"
    assert "ask_user" not in observed["nodes"]["analyst"]["tools"]
