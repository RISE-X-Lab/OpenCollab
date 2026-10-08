"""Public Team delivery includes a committed repair after Git integration."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters import worktree_pool
from opencollab.adapters.env import ContainerWorktreeEnvironment, DockerEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import container
from tests.support.docker_native_edits_support import LocalDockerTransport


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _response(name: str | None = None, arguments: dict | None = None) -> LLMResponse:
    calls = [] if name is None else [{
        "id": "scripted-call", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]
    return LLMResponse(
        content=None if calls else "Repair complete.", tool_calls=calls,
        usage=Usage(10, 5), finish_reason="tool_calls" if calls else "stop",
    )


@pytest.mark.parametrize("operation", [
    "control", "rebase", "cherry-pick", "merge", "cherry-pick-abort", "rebase-own-history", "reset-own-history",
])
async def test_team_delivers_the_repair_after_integrating_upstream(tmp_path, monkeypatch, operation):
    source = tmp_path / "repo"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "OpenCollab Tests")
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "commit.gpgsign", "false")
    (source / "answer.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "base")
    creation_base = _git(source, "rev-parse", "HEAD")
    transport = LocalDockerTransport(source, pair_reads=False)
    environment = DockerEnvironment(
        workspace=str(source), container_id="c" * 64, exec_workdir=str(source),
    )
    transport.attach(environment)
    original_init = ContainerWorktreeEnvironment.__init__

    def attach_transport(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        transport.attach(self)

    monkeypatch.setattr(ContainerWorktreeEnvironment, "__init__", attach_transport)
    monkeypatch.setattr(worktree_pool, "CONTAINER_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    config = tmp_path / "team.yaml"
    config.write_text("""entry: analyst
roles:
  analyst:
    prompt: You are the Analyst. Delegate the repair to the coder.
    tools: [spawn_agent]
  coder:
    prompt: You are the Coder. Repair and verify the answer.
    tools: [bash]
topology:
  analyst: [coder]
  coder: [analyst]
""", encoding="utf-8")
    observed = {}

    class ScriptedModel:
        def __init__(self, **_kwargs):
            self.turn = 0

        def context_window(self):
            return 200_000

        async def close(self):
            pass

        async def complete(self, messages, **_kwargs):
            self.turn += 1
            coder = "You are the Coder" in str(messages[0].get("content", ""))
            if not coder and self.turn == 1:
                return _response("spawn_agent", {
                    "role": "coder", "task": "Fix answer and integrate the helper.",
                })
            if coder and self.turn == 1:
                (source / "upstream.txt").write_text("helper\n", encoding="utf-8")
                _git(source, "add", "upstream.txt")
                _git(source, "commit", "-qm", "helper")
                observed["upstream"] = _git(source, "rev-parse", "HEAD")
                preparation = ""
                integration = {
                    "control": "git status --porcelain",
                    "rebase": "git rebase main",
                    "cherry-pick": f"git cherry-pick {observed['upstream']}",
                    "merge": "git merge --no-edit main",
                }.get(operation, "")
                if operation == "cherry-pick-abort":
                    (source / "answer.py").write_text("def answer():\n    return 3\n", encoding="utf-8")
                    _git(source, "add", "answer.py")
                    _git(source, "commit", "-qm", "conflicting answer")
                    conflicting = _git(source, "rev-parse", "HEAD")
                    integration = (
                        f"(git cherry-pick {observed['upstream']} {conflicting}; pick_status=$?; "
                        'test "$pick_status" -eq 1 && test -f upstream.txt && git cherry-pick --abort)'
                    )
                elif operation in {"rebase-own-history", "reset-own-history"}:
                    preparation = "printf extra > extra.txt && git add extra.txt && git commit -qm extra && "
                    if operation == "rebase-own-history":
                        preparation += "printf other > other.txt && git add other.txt && git commit -qm other && "
                        editor = shlex.quote("sed -i.bak '2s/^pick/fixup/'")
                        integration = f"git -c sequence.editor={editor} -c core.editor=true rebase -i HEAD~2"
                    else:
                        integration = "git reset --hard HEAD~1"
                command = (
                    "printf 'def answer():\\n    return 2\\n' > answer.py && "
                    "git add answer.py && git commit -qm repair && "
                    f"{preparation}{integration} && {shlex.quote(sys.executable)} -B -c "
                    "'from answer import answer; assert answer() == 2; print(answer())'"
                )
                return _response("bash", {"command": command})
            observed["coder" if coder else "parent"] = [
                str(message.get("content", "")) for message in messages if message.get("role") != "system"
            ]
            return _response()

    monkeypatch.setattr(container, "LLMClient", ScriptedModel)
    artifacts = tmp_path / "evidence"
    result = await OpenCollab(
        source, config={"model": "scripted", "provider": "openai"}, environment=environment,
    ).team(
        "Repair the answer.", config=config, budget=50_000, timeout=15, cleanup_timeout=3,
        use_worktrees=True,
        serialize_turns=True, artifacts=artifacts, trace=True,
    )
    assert result.status == "completed"
    assert not result.agent_failures
    assert any("answer.py" in message and "+    return 2" in message for message in observed["parent"])
    assert any("exit code: 0" in message.lower() and "2" in message for message in observed["coder"])
    records = [json.loads(line) for line in (artifacts / "trajectory.jsonl").read_text().splitlines()]
    changes = [record["payload"] for record in records if record["type"] == "worktree_changes"]
    coder_changes = [payload for payload in changes if payload["role"] == "coder"]
    assert coder_changes
    expected_files = {"answer.py"}
    if operation == "cherry-pick":
        expected_files.add("upstream.txt")
    elif operation == "rebase-own-history":
        expected_files.update({"extra.txt", "other.txt"})
    for payload in coder_changes:
        assert payload["diff_chars"] > 0
        assert expected_files == {entry["path"] for entry in payload["files"]}
        assert payload["head_commit"] in payload["commits"]
        assert payload["diff_base"] != payload["head_commit"]
        if operation in {"rebase", "merge"}:
            assert payload["diff_base"] == observed["upstream"]
        else:
            assert payload["diff_base"] == creation_base
    source_value = 3 if operation == "cherry-pick-abort" else 1
    assert (source / "answer.py").read_text(encoding="utf-8") == f"def answer():\n    return {source_value}\n"
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
