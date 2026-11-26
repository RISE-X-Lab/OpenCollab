"""Saved public workflows keep their live records outside candidate changes."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.working_tree import MAX_WORKING_TREE_DIFF_CHARS
from opencollab.application.workflow_candidates import CandidateWorkspaceTrackingError
from opencollab.tools import builtin_tools
from tests.support.duo_sandbox_test_support import simulate_candidate_sandbox
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


class EditingModel:
    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate

    async def complete(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            if self.mutate is not None:
                self.mutate()
            return LLMResponse(tool_calls=[{
                "id": "edit", "type": "function", "function": {
                    "name": "file_write", "arguments": json.dumps({
                        "path": "source.py", "mode": "create", "overwrite": True,
                        "content": "value = 2\n",
                    }),
                },
            }], usage=Usage(4, 2), finish_reason="tool_calls")
        assert any(message.get("tool_call_id") == "edit" for message in messages)
        return LLMResponse(content="done", usage=Usage(4, 2), finish_reason="stop")

@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("mode", ["agent", "workflow"])
@pytest.mark.parametrize("layout", ["root", "nested", "sibling", "external"])
async def test_public_candidate_adopts_with_saved_runtime_records(tmp_path, trace, mode, layout):
    repo = _repository(tmp_path)
    workspace = repo
    if layout in {"nested", "sibling"}:
        workspace = repo / "package"
        workspace.mkdir()
        (workspace / "source.py").write_text("value = 1\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "Add candidate subdirectory")
    artifacts = (
        tmp_path / "records" if layout == "external" else
        (repo if layout == "sibling" else workspace) / "records [current]"
    )
    index = (repo / ".git" / "index").read_bytes()
    captured = []

    async def edit(ctx, _inputs):
        return await ctx.agent("edit source.py", tools=builtin_tools("file_write"))

    async def flow(ctx, inputs):
        if mode == "agent":
            candidate = await ctx.candidate_agent(
                "edit source.py", label="edit", tools=builtin_tools("file_write"),
            )
        else:
            candidate = await ctx.candidate_workflow(edit, inputs, label="edit")
        captured.append(candidate)
        assert (workspace / "source.py").read_text() == "value = 1\n"
        assert await ctx.source_changed() is False
        assert "records" not in (await ctx.diff())
        await ctx.adopt_candidate(candidate)
        assert await ctx.source_changed() is True
        assert await ctx.source_changed(["source.py"]) is False
        return candidate.diff

    model = EditingModel()
    result = await OpenCollab(workspace, model="controlled-model").workflow(
        flow, llm=model, artifacts=artifacts, trace=trace,
        limit_mode="explicit", budget=10000, max_steps=4, timeout=20,
    )
    assert result.ok, result.reason
    assert model.calls == 2
    assert "+value = 2" in captured[0].diff
    assert "records" not in captured[0].diff
    assert (repo / ".git" / "index").read_bytes() == index
    assert (artifacts / "workflow.json").is_file()
    assert (artifacts / "orchestration.jsonl").exists() is trace
    subprocess.run(
        [sys.executable, "-c", "from source import value; assert value == 2"],
        cwd=workspace, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True,
    )

@pytest.mark.parametrize("mutation", ["tracked", "untracked", "head"])
async def test_saved_candidate_still_rejects_concurrent_user_changes(tmp_path, mutation):
    repo = _repository(tmp_path)
    note = repo / ("source.py" if mutation == "tracked" else "user.txt")
    index = (repo / ".git" / "index").read_bytes()
    head = _git(repo, "rev-parse", "HEAD").strip()

    def mutate():
        if mutation == "head":
            _git(repo, "commit", "--allow-empty", "-m", "Concurrent user commit")
        else:
            note.write_text("user edit\n")

    async def flow(ctx, _inputs):
        candidate = await ctx.candidate_agent(
            "edit source.py", label="edit", tools=builtin_tools("file_write"),
        )
        await ctx.adopt_candidate(candidate)

    result = await OpenCollab(repo, model="controlled-model").workflow(
        flow, llm=EditingModel(mutate), artifacts=repo / "records",
        trace=False, limit_mode="explicit", budget=10000, max_steps=4, timeout=20,
    )
    assert isinstance(result.error, CandidateWorkspaceTrackingError)
    assert "source worktree changed" in result.reason
    assert (repo / ".git" / "index").read_bytes() == index
    if mutation == "head":
        assert _git(repo, "rev-parse", "HEAD").strip() != head
    else:
        assert note.read_text() == "user edit\n"
    if mutation != "tracked":
        assert (repo / "source.py").read_text() == "value = 1\n"

@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("internal", [False, True])
async def test_builtin_duo_selects_and_adopts_with_saved_runtime_records(tmp_path, monkeypatch, trace, internal):
    simulate_candidate_sandbox(monkeypatch)
    repo = _repository(tmp_path)
    (repo / "test_public.py").write_text("from source import value\ndef test_value():\n    assert value == 2\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add native candidate test")
    index = (repo / ".git" / "index").read_bytes()
    command = f"{shlex.quote(sys.executable)} -m pytest -q -rA -p no:cacheprovider test_public.py"

    class DuoModel:
        calls = 0

        def context_window(self):
            return 1_048_576

        async def complete(self, messages, **_kwargs):
            self.calls += 1
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            candidate_a = "You are candidate A," in prompt
            step = sum(message["role"] == "tool" for message in messages)
            actions = [
                ("file_write", {"path": "source.py", "mode": "create", "overwrite": True,
                                "content": f"value = {3 if candidate_a else 2}\n"}),
                ("bash", {"command": command}),
            ]
            if step < len(actions):
                name, arguments = actions[step]
                return LLMResponse(tool_calls=[{
                    "id": f"call-{self.calls}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments),
                    },
                }], usage=Usage(4, 2), finish_reason="tool_calls")
            return LLMResponse(content="done", usage=Usage(4, 2), finish_reason="stop")

    artifacts = (repo if internal else tmp_path) / "records"
    model = DuoModel()
    result = await OpenCollab(repo, model="controlled-model").workflow(
        "duo", {"goal": "source.py value must equal two", "allow_unisolated_shell": True,
                "submission_mode": "working_tree", "candidate_evidence_dir": str(repo / "evidence")},
        llm=model, artifacts=artifacts, trace=trace, limit_mode="explicit",
        budget=10000, max_steps=5, timeout=20,
    )
    assert result.ok, result.reason
    assert model.calls == 6
    assert result.output["winner"] == result.output["adopted"] == "B"
    for label, code in (("A", 1), ("B", 0)):
        candidate = result.output["candidates"][label]
        assert candidate["changed_paths"] == ["source.py"]
        assert candidate["public_test_records"][0]["exit_code"] == code
        assert candidate["public_test_records"][0]["verified"] is (code == 0)
    assert (repo / ".git" / "index").read_bytes() == index
    verification = subprocess.run(
        shlex.split(command), cwd=repo, capture_output=True, text=True, check=True,
    )
    assert "1 passed" in verification.stdout

@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("size_mode", ["single", "aggregate"])
async def test_saved_source_diff_preserves_complete_evidence_limit(tmp_path, saved, size_mode):
    repo = _repository(tmp_path)
    divisor, count = (12, 1) if size_mode == "single" else (100, 10)
    contents = "user content\n" * (MAX_WORKING_TREE_DIFF_CHARS // divisor)
    users = [repo / f"large-user-{index}.txt" for index in range(count)]
    for user in users:
        user.write_text(contents)
    index = (repo / ".git" / "index").read_bytes()

    async def flow(ctx, _inputs):
        assert await ctx.source_changed() is True
        return await ctx.diff()

    result = await OpenCollab(repo).workflow(
        flow, artifacts=repo / "records" if saved else None, trace=False,
    )
    assert result.ok
    assert result.output is None
    assert all(user.read_text() == contents for user in users)
    assert (repo / ".git" / "index").read_bytes() == index

@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("source_changed", [False, True])
async def test_saved_subdirectory_status_keeps_workspace_relative_exclusions(tmp_path, saved, source_changed):
    repo = _repository(tmp_path)
    workspace = repo / "package"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add source status subdirectory")
    injected = workspace / "tests" / "injected.py"
    injected.parent.mkdir()
    injected.write_text("def test_injected():\n    assert True\n")
    if source_changed:
        source.write_text("value = 2\n")
    index = _git(repo, "ls-files", "--stage")
    head = _git(repo, "rev-parse", "HEAD")

    async def flow(ctx, _inputs):
        assert await ctx.source_changed() is True
        return await ctx.source_changed(["tests/injected.py"])

    result = await OpenCollab(workspace).workflow(
        flow, artifacts=tmp_path / "records" if saved else None, trace=False,
    )
    assert result.ok, result.reason
    assert result.output is source_changed
    assert source.read_text() == ("value = 2\n" if source_changed else "value = 1\n")
    assert _git(repo, "ls-files", "--stage") == index
    assert _git(repo, "rev-parse", "HEAD") == head
