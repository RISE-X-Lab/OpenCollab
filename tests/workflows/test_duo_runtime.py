"""Duo executes real local tools, isolated candidates, tests, and adoption."""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import _workflow_runtime_session as workflow_session
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.workflow_runtime import run_workflow
from opencollab.builtin_workflows import _file_selection


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True,
    ).stdout


def _repository(path, *, both_pass):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "source.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    comparison = ">= 2" if both_pass else "== 3"
    (path / "test_public.py").write_text(
        "from source import value\n\ndef test_public():\n"
        f"    assert value() {comparison}\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "\u521d\u59cb\u6d4b\u8bd5\u4ed3\u5e93")
    return path


def _tool_response(name, arguments, sequence):
    return LLMResponse(
        content=None,
        tool_calls=[{
            "id": f"call-{sequence}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
        finish_reason="tool_calls", usage=Usage(5, 3),
    )


def _last_tool_result(messages):
    return next(message["content"] for message in reversed(messages) if message["role"] == "tool")


class _ScriptedCoder:
    def __init__(self, role, command, source):
        self.role = role
        self.command = command
        self.source = source
        self.calls = []

    def context_window(self):
        return 1_048_576

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        assert self.source.read_text() == "def value():\n    return 1\n"
        step = len(self.calls)
        if step == 1:
            return _tool_response("file_read", {"path": "source.py"}, step)
        if step == 2:
            assert "return 1" in _last_tool_result(messages)
            content = "def value():\n    return " + ("2" if self.role == "A" else "3") + "\n"
            return _tool_response("file_write", {"path": "source.py", "mode": "create", "content": content}, step)
        if step == 3:
            return _tool_response("bash", {"command": self.command}, step)
        return LLMResponse(content=f"Candidate {self.role} finished", usage=Usage(5, 3))


class _ScriptedJudge:
    def __init__(self, *, file_evidence):
        self.file_evidence = file_evidence
        self.calls = []

    def context_window(self):
        return 1_048_576

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        step = len(self.calls)
        if not tools:
            return LLMResponse(content="Role context received", usage=Usage(5, 3))
        if self.file_evidence and step <= 2:
            role = "A" if step == 1 else "B"
            return _tool_response("read_candidate_evidence", {"path": f"{role}/candidate.diff"}, step)
        return _tool_response("structured_output", {
            "winner": "B", "requirements_complete": True,
            "requirements": [{
                "requirement": "value() must return 3",
                "a_coverage": "not_covered", "b_coverage": "covered",
                "a_evidence": ["source.py changes value() to return 2"],
                "b_evidence": ["source.py changes value() to return 3"],
            }],
            "rationale": "The source.py diff for B returns the required value 3",
        }, step)


def _script_sessions(monkeypatch, source, command):
    sessions = []
    original = workflow_session.build_session

    def build(**kwargs):
        names = [tool.name for tool in kwargs["agent"].tools]
        if "bash" in names:
            role = "A" if not sessions else "B"
            llm = _ScriptedCoder(role, command, source)
        else:
            llm = _ScriptedJudge(file_evidence="read_candidate_evidence" in names)
        session = original(**kwargs, llm=llm)
        sessions.append((session, kwargs, llm))
        return session

    monkeypatch.setattr(workflow_session, "build_session", build)
    return sessions


@pytest.mark.parametrize("profile", [None, "single2"])
@pytest.mark.parametrize("flow_name,both_pass", [("duo", False), ("duo", True)])
@pytest.mark.parametrize("file_evidence", [False, True])
async def test_duo_named_sdk_executes_candidates_and_adopts_verified_patch(
    tmp_path, monkeypatch, profile, flow_name, both_pass, file_evidence,
):
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    if file_evidence:
        monkeypatch.setattr(_file_selection, "_INLINE_EVIDENCE_MAX_BYTES", 0)
    repo = _repository(tmp_path / "repo", both_pass=both_pass)
    command = f"{shlex.quote(sys.executable)} -m pytest -q -rA -p no:cacheprovider test_public.py"
    sessions = _script_sessions(monkeypatch, repo / "source.py", command)
    evidence = tmp_path / "evidence"
    result = await OpenCollab(repo, model="scripted-local-model", provider="openai").workflow(
        flow_name,
        {"goal": "value() must return 3", "candidate_evidence_dir": str(evidence), "allow_unisolated_shell": True},
        agent_profile=profile, budget=10_000, max_steps=8, trace=False,
    )

    assert result.ok, result.error
    assert result.output["status"] == "done", (result.output, result.agent_failures)
    assert result.output["winner"] == result.output["adopted"] == "B"
    assert result.output["shared_public_command"] == command
    assert result.output["judge_used"] is both_pass
    assert result.output["selection_reason"] == ("contract-adjudicated" if both_pass else "same-command-public-red")
    for role in ("A", "B"):
        candidate = result.output["candidates"][role]
        assert candidate["changed_paths"] == ["source.py"]
        assert candidate["public_command"] == command
        records = candidate["public_test_records"]
        assert len(records) == 1
        assert records[0]["target"] == "test_public.py"
        assert records[0]["command"] == command
        green = role == "B" or both_pass
        assert records[0]["verified"] is green
        assert records[0]["exit_code"] == (0 if green else 1)
    assert (repo / "source.py").read_text() == "def value():\n    return 3\n"
    assert _git(repo, "diff", "--name-only").splitlines() == ["source.py"]
    assert result.metrics["sessions"] == (3 if both_pass else 2)
    coder_workspaces = [Path(kwargs["env"].workspace) for _, kwargs, _ in sessions[:2]]
    assert len(set(coder_workspaces)) == 2
    assert all(workspace != repo and not workspace.exists() for workspace in coder_workspaces)
    assert "return 1" in _last_tool_result(sessions[1][2].calls[1])
    assert command in str(sessions[1][2].calls[0])
    for session, kwargs, _ in sessions:
        actual_profile = kwargs["agent_profile"]
        assert (None if actual_profile is None else actual_profile.name) == profile
        if profile == "single2":
            assert session.agent.system_prompt.startswith(SINGLE2_SYSTEM_PROMPT)
            assert "They take precedence over the general software-repair duties" in session.agent.system_prompt
    if both_pass:
        judge, _, scripted = sessions[2]
        names = [tool.name for tool in judge.agent.tools]
        if file_evidence:
            assert set(names) == {"read_candidate_evidence", "structured_output"}
            assert len(scripted.calls) == 3
            assert "return 2" in _last_tool_result(scripted.calls[1])
            assert "return 3" in _last_tool_result(scripted.calls[2])
        else:
            assert names == ["structured_output"]
            assert len(scripted.calls) == 1
            prompt = next(message["content"] for message in scripted.calls[0]
                          if "\nCandidate evidence\n" in message.get("content", ""))
            payload, _ = json.JSONDecoder().raw_decode(prompt.split("\nCandidate evidence\n", 1)[1])
            for role, value in [("A", "2"), ("B", "3")]:
                inline = payload["inline_comparison"][role]
                assert f"return {value}" in inline["diff"]
                assert inline["candidate_report"] == f"Candidate {role} finished"
                assert inline["public_test_records"] == result.output["candidates"][role]["public_test_records"]
                assert inline["report_is_model_supplied"] is True
        directories = list(evidence.glob("duo-evidence-*"))
        assert len(directories) == 1
        assert "return 3" in (directories[0] / "B/candidate.diff").read_text()
    completed = subprocess.run(shlex.split(command), cwd=repo, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout


async def test_runtime_resolves_cli_profile_name_in_composition_root(tmp_path, monkeypatch):
    observed = []
    original = workflow_session.build_session

    def build(**kwargs):
        observed.append(kwargs["agent_profile"])
        return original(**kwargs, llm=_ScriptedJudge(file_evidence=False))

    monkeypatch.setattr(workflow_session, "build_session", build)

    async def flow(ctx, args):
        assert await ctx.agent("Read the role context", tools=[]) == "Role context received"
        return {"profile": "single2"}

    result = await run_workflow(
        flow, {}, cfg={"model": "scripted-local-model", "provider": "openai", "api_key": None, "budget": 10_000},
        workspace=str(tmp_path), agent_profile="single2", max_steps=3, trace=False,
    )
    assert result == {"profile": "single2"}
    assert [profile.name for profile in observed] == ["single2"]


@pytest.mark.parametrize(("filename", "desired"), [
    ("app.conf", "enabled=true\n"),
    ("report.csv", "item,count\nready,3\n"),
])
async def test_duo_delivers_task_configuration_and_data_artifacts(tmp_path, monkeypatch, filename, desired):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    repo = tmp_path / "artifacts"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / filename).write_text("initial\n")
    (repo / "test_delivery.py").write_text(
        "from pathlib import Path\n\ndef test_delivery():\n"
        f"    assert Path({filename!r}).read_text() == {desired!r}\n"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "\u521d\u59cb\u4ea4\u4ed8\u6837\u4f8b")
    command = f"{shlex.quote(sys.executable)} -m pytest -q -rA -p no:cacheprovider test_delivery.py"
    original = workflow_session.build_session
    calls = []

    class Solver:
        def __init__(self, role):
            self.role = role
            self.step = 0

        def context_window(self):
            return 1_048_576

        async def complete(self, messages, tools=None, **kwargs):
            assert (repo / filename).read_text() == "initial\n"
            self.step += 1
            if self.step == 1:
                assert "configuration, dependencies" in str(messages)
                return _tool_response("file_write", {
                    "path": filename, "mode": "create",
                    "content": "partial\n" if self.role == "A" else desired,
                }, self.step)
            if self.step == 2:
                return _tool_response("bash", {"command": command}, self.step)
            return LLMResponse(content="Requested artifact retained", usage=Usage(5, 3))

    def build(**kwargs):
        role = "A" if not calls else "B"
        calls.append(role)
        return original(**kwargs, llm=Solver(role))

    monkeypatch.setattr(workflow_session, "build_session", build)
    result = await OpenCollab(repo, provider="openai", model="scripted-model").workflow(
        "duo", {"goal": f"Produce {filename} with the requested content", "allow_unisolated_shell": True},
        budget=10_000, max_steps=5, trace=False,
    )
    assert result.ok and result.output["status"] == "done"
    assert result.output["selection_reason"] == "same-command-public-red"
    assert result.output["adopted"] == "B"
    assert calls == ["A", "B"]
    assert (repo / filename).read_text() == desired
    assert _git(repo, "diff", "--name-only").strip() == filename
