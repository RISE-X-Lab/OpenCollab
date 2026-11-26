"""Final candidate comparison retains test history after observed native edits."""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import _workflow_runtime_session as workflow_session
from tests.support.duo_sandbox_test_support import simulate_candidate_sandbox


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout

def _repository(path, case):
    path.mkdir()
    (path / "module.py").write_text("VALUE = 0\n")
    (path / "test_public.py").write_text(
        "from module import VALUE\n\ndef test_expected_value():\n    assert VALUE == 2\n"
    )
    (path / ".gitignore").write_text("__pycache__/\n.tmp-tests/\n.pytest_cache/\n")
    if case == "directory-rejection":
        (path / "docs").mkdir()
        (path / "docs" / "guide.md").write_text("Guide\n")
    if case in {"oversized-write", "oversized-patch"}:
        header = "START\n"
        (path / "large.txt").write_text(header + "x" * (ENV_FILE_WRITE_LIMIT_BYTES - len(header)))
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "add", ".")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-qm", "\u521b\u5efa\u72ec\u7acb\u6f14\u7ec3")

def _tool_response(name, params, index):
    return LLMResponse(
        content=None,
        tool_calls=[{
            "id": f"call-{index}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(params)},
        }],
        finish_reason="tool_calls", usage=Usage(1, 1),
    )

def _replace(old, new):
    return "file_write", {
        "path": "module.py", "mode": "str_replace", "old_str": f"VALUE = {old}", "new_str": f"VALUE = {new}",
    }

def _actions(case, command):
    run = "bash", {"command": command}
    a = [_replace(0, 2), run]
    b = [run, _replace(0, 2)]
    if case in {"stale", "a-retested", "b-retested", "both-retested", "b-equivalent-retested", "patch-stale"}:
        a.append(_replace(2, 3))
    if case == "patch-stale":
        a[-1] = "apply_patch", {"path": "module.py", "mode": "line_replace", "start_line": 1,
                                "end_line": 1, "new_str": "VALUE = 3\n"}
    if case in {"a-retested", "both-retested"}:
        a.append(run)
    if case in {"b-retested", "both-retested"}:
        b.append(run)
    if case == "b-equivalent-retested":
        b.append(("bash", {"command": command + " -vv"}))
    if case in {"fresh", "notes", "failed-edit", "noop-edit", "unchanged-write",
                "directory-rejection", "oversized-write", "oversized-patch"}:
        b = [_replace(0, 1), run]
    if case == "notes":
        a.append(("file_write", {"path": "notes.md", "mode": "create", "content": "Task notes\n"}))
    if case == "failed-edit":
        a.append(_replace(999, 3))
    if case == "noop-edit":
        a.append(_replace(2, 2))
    if case == "unchanged-write":
        a.append(("file_write", {"path": "module.py", "mode": "create", "content": "VALUE = 2\n",
                                 "overwrite": True}))
    if case == "directory-rejection":
        a.append(("file_write", {"path": "docs", "mode": "create", "content": "Guide\n", "overwrite": True}))
    if case == "oversized-write":
        a.append(("file_write", {"path": "large.txt", "mode": "str_replace", "old_str": "START",
                                 "new_str": "longer header"}))
    if case == "oversized-patch":
        a.append(("apply_patch", {"path": "large.txt", "mode": "line_replace", "start_line": 1,
                                  "end_line": 1, "new_str": "longer header"}))
    if case in {"shell-stale", "shell-retested", "shell-read"}:
        b = [_replace(0, 1), run]
        command_after_test = "cat module.py" if case == "shell-read" else "printf 'VALUE = 3\\n' > module.py"
        a.append(("bash", {"command": command_after_test}))
        if case == "shell-retested":
            a.extend([("bash", {"command": "printf 'VALUE = 2\\n' > module.py"}), run])
    return a, b

class _ScriptedModel:
    def __init__(self, *, actions=None, winner="B"):
        self.actions = actions
        self.winner = winner
        self.calls = []

    def context_window(self):
        return 1_048_576

    async def complete(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        index = len(self.calls) - 1
        if self.actions is not None:
            if index < len(self.actions):
                return _tool_response(*self.actions[index], index)
            return LLMResponse(content="Recorded executed checks and edits", usage=Usage(1, 1))
        covered = "a_coverage" if self.winner == "A" else "b_coverage"
        missing = "b_coverage" if self.winner == "A" else "a_coverage"
        return _tool_response("structured_output", {
            "winner": self.winner, "requirements_complete": True,
            "requirements": [{
                "requirement": "Return VALUE 2", covered: "covered", missing: "not_covered",
                "a_evidence": ["module.py contains the candidate A value"],
                "b_evidence": ["module.py contains the candidate B value"],
            }],
            "rationale": "Inspect the final module.py contents",
        }, index)

@pytest.mark.parametrize("case,winner,mechanical", [
    ("stale", "B", False),
    ("patch-stale", "B", False),
    ("a-retested", "B", False),
    ("b-retested", "B", False),
    ("both-retested", "B", True),
    ("b-equivalent-retested", "B", False),
    ("fresh", "A", True),
    ("notes", "A", False),
    ("failed-edit", "A", True),
    ("noop-edit", "A", True),
    ("unchanged-write", "A", True),
    ("directory-rejection", "A", True),
    ("oversized-write", "A", True),
    ("oversized-patch", "A", True),
    ("shell-stale", "B", False),
    ("shell-retested", "A", True),
    ("shell-read", "A", False),
])
async def test_duo_compares_execution_applicable_to_final_candidate(tmp_path, monkeypatch, case, winner, mechanical):
    simulate_candidate_sandbox(monkeypatch)
    monkeypatch.delenv("OPENCOLLAB_WORKFLOWS_DIR", raising=False)
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    repo = tmp_path / "repo"
    _repository(repo, case)
    command = (f"PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -m pytest -q -rA "
               "-p no:cacheprovider --basetemp=.tmp-tests test_public.py")
    actions = _actions(case, command)
    sessions = []
    original = workflow_session.build_session

    def build(**kwargs):
        names = [tool.name for tool in kwargs["agent"].tools]
        llm = (_ScriptedModel(actions=actions[len(sessions)]) if "bash" in names
               else _ScriptedModel(winner=winner))
        session = original(**kwargs, llm=llm)
        sessions.append((session, llm))
        return session

    monkeypatch.setattr(workflow_session, "build_session", build)
    result = await OpenCollab(repo, model="scripted-local-model", provider="openai").workflow(
        "duo", {"goal": "Set VALUE to 2 and retain the public test", "allow_unisolated_shell": True,
                "candidate_evidence_dir": str(tmp_path / "evidence")},
        budget=10_000, max_steps=8, trace=False,
    )
    assert result.ok, result.error
    output = result.output
    assert output["winner"] == output["adopted"] == winner
    assert output["judge_used"] is not mechanical
    assert output["selection_reason"] == ("same-command-public-red" if mechanical else "contract-adjudicated")
    a = output["candidates"]["A"]["public_test_records"]
    b = output["candidates"]["B"]["public_test_records"]
    assert (a[0]["exit_code"], a[0]["verified"]) == (0, True)
    assert (b[0]["exit_code"], b[0]["verified"]) == (1, False)
    if case in {"directory-rejection", "oversized-write", "oversized-patch"}:
        expected_error = "target is not a regular file" if case == "directory-rejection" else "exceeds write limit"
        a_tool_results = sessions[0][1].calls[-1]
        assert any(message["role"] == "tool" and expected_error in message["content"] for message in a_tool_results)
        assert a[0]["applicability"] == "current"
        assert a[0]["post_test_edits"] == []
    if case in {"shell-stale", "shell-retested", "shell-read"}:
        assert a[0]["applicability"] == "unknown"
        assert a[0]["post_test_edits"] == []
        assert a[0]["post_test_commands"]
    if case == "shell-retested":
        assert a[-1]["verified"] is True
        assert a[-1]["applicability"] == "current"
    if case in {"stale", "patch-stale", "a-retested", "b-retested", "both-retested", "b-equivalent-retested", "notes"}:
        assert a[0]["applicability"] == "unknown"
        assert a[0]["post_test_edits"] == ["notes.md" if case == "notes" else "module.py"]
    if case in {"stale", "patch-stale", "a-retested", "b-retested", "both-retested", "b-equivalent-retested"}:
        assert b[0]["applicability"] == "unknown"
        assert b[0]["post_test_edits"] == ["module.py"]
    if case in {"a-retested", "both-retested"}:
        assert len(a) == 2 and a[-1]["applicability"] == "current"
        assert (a[-1]["exit_code"], a[-1]["verified"]) == (1, False)
    if case in {"b-retested", "both-retested", "b-equivalent-retested"}:
        assert len(b) == 2 and b[-1]["applicability"] == "current"
        assert (b[-1]["exit_code"], b[-1]["verified"]) == (0, True)
        assert b[-1]["command"] == command + (" -vv" if case == "b-equivalent-retested" else "")
    if not mechanical:
        messages = sessions[2][1].calls[0]
        prompt = next(message["content"] for message in messages
                      if "\nCandidate evidence\n" in message.get("content", ""))
        payload, _ = json.JSONDecoder().raw_decode(prompt.split("\nCandidate evidence\n", 1)[1])
        for role, history in [("A", a), ("B", b)]:
            assert payload["inline_comparison"][role]["public_test_records"] == history
    completed = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True)
    if case == "shell-stale":
        # Both final candidates fail. Their historical records cannot invent a
        # passing final result, and the configured adjudicator selected B.
        assert completed.returncode == 1
        assert "1 failed" in completed.stdout
        assert (repo / "module.py").read_text() == "VALUE = 1\n"
        return
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    assert (repo / "module.py").read_text() == "VALUE = 2\n"
