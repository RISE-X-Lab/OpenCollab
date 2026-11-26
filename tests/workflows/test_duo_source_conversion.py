from __future__ import annotations

import json
import shlex
import subprocess
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage
from tests.support.duo_sandbox_test_support import simulate_candidate_sandbox
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


async def _run(tmp_path, monkeypatch, initial, final, *, crlf=False, mutation=None):
    simulate_candidate_sandbox(monkeypatch)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    source = _repository(tmp_path)
    _git(source, "config", "core.autocrlf", str(initial).lower())
    (source / ".gitattributes").write_text("*.diff text eol=lf\n")
    original = b"value = 1\r\n" if crlf else b"value = 1\n"
    (source / "source.py").write_bytes(original)
    (source / "notes.txt").write_text("original\n")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "Prepare source conversion change")
    (source / "notes.txt").write_text("staged\n")
    _git(source, "add", "notes.txt")
    (source / "notes.txt").write_text("user edit\n")
    (source / "user.txt").write_text("user file\n")
    index = (source / ".git/index").read_bytes()
    head = _git(source, "rev-parse", "HEAD")
    calls = 0
    tested = []

    async def complete(llm, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            tested.extend(message.get("content") for message in messages if message.get("role") == "tool")
        if calls in (1, 4):
            name = "file_write"
            arguments = {"path": "app.py", "mode": "create",
                         "content": "print(43)\n" if calls == 4 and mutation else "print(42)\n"}
        elif calls == 2:
            name = "bash"
            arguments = {"command": f"git config core.autocrlf {str(final).lower()} && "
                         f"{shlex.quote(sys.executable)} -B app.py"}
        elif calls in (3, 5):
            return LLMResponse(content="Done", usage=Usage(1, 1), finish_reason="stop")
        else:
            if mutation == "bytes":
                (source / "source.py").write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            elif mutation == "staged":
                _git(source, "add", "notes.txt")
            elif mutation == "unstaged":
                _git(source, "reset", "--", "notes.txt")
            elif mutation == "untracked-add":
                (source / "later.txt").write_text("later user file\n")
            elif mutation == "untracked-remove":
                (source / "user.txt").unlink()
            elif mutation == "head":
                _git(source, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Move source revision")
            name = "structured_output"
            arguments = {"winner": "B", "requirements_complete": True, "requirements": [
                {"requirement": "app.py prints 43", "a_coverage": "not_covered", "b_coverage": "covered",
                 "a_evidence": ["app.py prints 42"], "b_evidence": ["app.py prints 43"]},
            ], "rationale": "B produces the required result"}
        return LLMResponse(tool_calls=[{"id": f"call-{calls}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments),
        }}], usage=Usage(1, 1), finish_reason="tool_calls")

    monkeypatch.setattr(LLMClient, "complete", complete)
    client = OpenCollab(source, model="test-model", provider="openai", api_key="test-key")  # pragma: allowlist secret
    result = await client.workflow(
        "duo", {"goal": "app.py prints 43" if mutation else "app.py prints 42",
                "allow_unisolated_shell": True, "candidate_evidence_dir": str(source / "evidence")},
        budget=100_000, max_steps=8, trace=False,
    )
    assert any("42" in str(message) for message in tested)
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
    if mutation:
        assert not result.ok
        assert "source worktree changed before candidate adoption" in result.reason
        assert not (source / "app.py").exists()
    else:
        assert result.ok, result.reason
        assert result.output["adopted"] == "B"
        assert calls == 5
        assert (source / "source.py").read_bytes() == original
        assert (source / "notes.txt").read_text() == "user edit\n"
        assert (source / "user.txt").read_text() == "user file\n"
        assert (source / ".git/index").read_bytes() == index
        assert _git(source, "rev-parse", "HEAD") == head
        executed = subprocess.run([sys.executable, "-B", "app.py"], cwd=source, capture_output=True, check=True)
        assert executed.stdout == b"42\n"
    return source


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("crlf", [False, True])
async def test_duo_source_configuration_compares_physical_contents(tmp_path, monkeypatch, initial, final, crlf):
    await _run(tmp_path, monkeypatch, initial, final, crlf=crlf)


@pytest.mark.parametrize("initial,final", [(False, True), (True, False)])
@pytest.mark.parametrize("mutation", ["bytes", "staged", "unstaged", "untracked-add", "untracked-remove", "head"])
async def test_duo_source_configuration_keeps_real_changes_visible(tmp_path, monkeypatch, initial, final, mutation):
    await _run(tmp_path, monkeypatch, initial, final, mutation=mutation)


async def test_duo_plain_source_skips_physical_capture(tmp_path, monkeypatch):
    from opencollab.adapters import _candidate_source

    async def reject_source_capture(*args, **kwargs):
        raise AssertionError("plain source captured a physical tree")

    monkeypatch.setattr(_candidate_source, "_physical_tree", reject_source_capture)
    monkeypatch.setattr(_candidate_source._SourceSnapshot, "_original_tree", reject_source_capture)
    await _run(tmp_path, monkeypatch, False, False)
