"""A team can be run against a repository the run's workspace does not hold.

The evaluation harness keeps the repository under test inside a container and
extracts the run's patch from a bounded archive of it. Exposing that repository
to the host would take a bind mount, which is exactly what such an extraction
cannot allow -- so an arm that could only work on the host workspace could not
be run there at all. ``client.team()`` used to refuse a supplied environment
outright.

Both wiring points are checked in one run, because either alone would leave the
team split across two places: agent 0 executes in the supplied environment, and
each teammate's worktree is cut from the same repository rather than from the
workspace the client was anchored to. A local environment stands in for the
container here -- what is under test is where the run works, not how a
container is reached.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.sdk import OpenCollab

ANALYST_AID, CODER_AID = 0, 1


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "which.txt").write_text(f"{marker}\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", marker)
    return path


def _team_file(path: Path) -> Path:
    path.write_text(
        """
entry: analyst
roles:
  analyst:
    prompt: You are the Analyst. Say where you are.
    tools: [bash, message_agent]
  coder:
    prompt: You are the Coder. Say where you are.
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
            read_marker = _call("t", "bash", {"command": "cat which.txt"})
            if self.turn == 1:
                return LLMResponse(
                    tool_calls=[read_marker], usage=usage, finish_reason="tool_calls"
                )
            if self.turn == 2:
                for message in reversed(messages):
                    if message.get("role") == "tool":
                        observed[f"{self.role}_saw"] = str(message.get("content") or "")
                        break
                if self.role == "analyst":
                    return LLMResponse(
                        tool_calls=[
                            _call(
                                "a2",
                                "message_agent",
                                {
                                    "to_aid": CODER_AID,
                                    "summary": "where are you",
                                    "content": "Say where you are.",
                                },
                            )
                        ],
                        usage=usage,
                        finish_reason="tool_calls",
                    )
            return LLMResponse(content="done", usage=usage, finish_reason="stop")

    return ScriptedLLM


@pytest.fixture
def run_team_in(tmp_path, monkeypatch):
    async def run(*, environment) -> dict:
        anchor = _repo(tmp_path / "anchor", "anchor-workspace")
        artifacts = tmp_path / "artifacts"
        observed: dict = {}
        monkeypatch.setattr(container, "LLMClient", _scripted_llm(observed))
        client = OpenCollab(
            anchor,
            config={
                "model": "gpt-4o",
                "provider": "openai",
                "api_key": "test-key",  # pragma: allowlist secret
                "base_url": None,
                "budget": 2_000_000,
            },
            environment=environment,
        )
        observed["result"] = await client.team(
            "Say where you are.",
            config=_team_file(tmp_path / "team.yaml"),
            artifacts=artifacts,
            trace=True,
            use_worktrees=True,
            prebuild_team=True,
            allow_unisolated_shell=True,
            serialize_turns=True,
        )
        observed["records"] = [
            json.loads(line)
            for line in (artifacts / "trajectory.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        return observed

    return run


async def test_a_team_works_in_the_environment_it_was_given(run_team_in, tmp_path):
    """Agent 0 and every teammate's worktree come from the supplied repository."""
    elsewhere = _repo(tmp_path / "elsewhere", "supplied-environment")

    observed = await run_team_in(environment=LocalEnvironment(str(elsewhere)))

    assert observed["analyst_saw"].strip().endswith("supplied-environment")
    assert observed["coder_saw"].strip().endswith("supplied-environment")
    assert "anchor-workspace" not in observed["analyst_saw"]
    assert "anchor-workspace" not in observed["coder_saw"]


async def test_a_team_given_no_environment_still_works_in_its_workspace(
    run_team_in, tmp_path
):
    """The control: without one, nothing about where the run works has changed."""
    observed = await run_team_in(environment=None)

    assert observed["analyst_saw"].strip().endswith("anchor-workspace")
    assert observed["coder_saw"].strip().endswith("anchor-workspace")


async def test_a_finished_team_run_says_it_wound_down(run_team_in):
    """Wind-down evidence, in the same fields a solo agent and a workflow use.

    Without it a caller cannot tell a team that finished from one abandoned
    mid-flight, and a harness that reads the absence as "not settled" throws
    away the workspace of every completed team run -- which is how this was
    found: every team run produced an empty patch and reported that its
    execution had not quiesced, while the identical workflow run did not.
    """
    metrics = (await run_team_in(environment=None))["result"].metrics

    assert metrics["session_quiesced"] is True
    assert metrics["environment_owned"] is True
    assert metrics["execution_quiesced"] is True


async def test_a_team_says_nothing_about_an_environment_it_was_lent(
    run_team_in, tmp_path
):
    """It never cleans a supplied environment, so it does not vouch for one."""
    elsewhere = _repo(tmp_path / "elsewhere", "supplied-environment")

    metrics = (await run_team_in(environment=LocalEnvironment(str(elsewhere))))[
        "result"
    ].metrics

    assert metrics["session_quiesced"] is True
    assert metrics["environment_owned"] is False
    assert metrics["environment_quiesced"] is None
