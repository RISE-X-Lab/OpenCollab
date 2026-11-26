"""Duo wrappers retain candidate provenance until the real adoption boundary."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow_candidates import CandidateWorkspaceTrackingError
from opencollab.bootstrap import _workflow_runtime_session as wiring
from opencollab.tools import evidence_tools
from tests.support.duo_sandbox_test_support import simulate_candidate_sandbox


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

def response(name, arguments, sequence):
    return LLMResponse(
        content=None,
        tool_calls=[
            {
                "id": f"call-{sequence}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
        finish_reason="tool_calls",
        usage=Usage(5, 3),
    )

@pytest.mark.parametrize("mode,mutate,physical", [
    ("duo", False, False), ("duo", True, False), ("direct", True, False),
    ("duo", False, True), ("duo", True, True),
])
async def test_duo_source_revision_survives_coder_wrapper(tmp_path, monkeypatch, mode, mutate, physical):
    simulate_candidate_sandbox(monkeypatch)
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "source.py").write_text("def value():\n    return 1\n")
    (repo / "flags.py").write_text("ENABLED = True\n")
    helper = b'#!/bin/sh\nprintf "42\\n"\n'
    if physical:
        (repo / ".gitattributes").write_text("*.sh text eol=crlf\n")
        (repo / "helper.sh").write_bytes(helper)
        (repo / "helper.sh").chmod(0o755)
    probe = '    subprocess.run(["./helper.sh"], check=True, stdout=subprocess.DEVNULL)\n' if physical else ""
    (repo / "test_public.py").write_text(
        "import subprocess\nfrom source import value\nfrom flags import ENABLED\n\n"
        f"def test_public():\n{probe}    assert ENABLED and value() >= 2\n"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "\u521d\u59cb\u5316\u5019\u9009\u4f9d\u8d56")
    original_branch = git(repo, "branch", "--show-current").strip()
    original_revision = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-qb", "other")
    (repo / "flags.py").write_text("ENABLED = False\n")
    git(repo, "commit", "-am", "\u4fee\u6539\u5019\u9009\u4f9d\u8d56")
    git(repo, "checkout", "-q", original_branch)
    source_index = (repo / ".git/index").read_bytes()
    command = f"{shlex.quote(sys.executable)} -m pytest -q -rA -p no:cacheprovider test_public.py"
    judge_entered = asyncio.Event()
    changed = asyncio.Event()
    sessions = []
    original_build = wiring.build_session

    class Coder:
        def __init__(self, value):
            self.value = value
            self.step = 0

        def context_window(self):
            return 1_048_576

        async def complete(self, messages, **kwargs):
            self.step += 1
            if self.step == 1:
                return response(
                    "file_write",
                    {
                        "path": "source.py",
                        "mode": "create",
                        "overwrite": True,
                        "content": f"def value():\n    return {self.value}\n",
                    },
                    self.step,
                )
            if self.step == 2:
                return response("bash", {"command": command}, self.step)
            return LLMResponse(content="candidate checked", usage=Usage(5, 3))

    class Judge:
        def context_window(self):
            return 1_048_576

        async def complete(self, messages, **kwargs):
            judge_entered.set()
            if mutate:
                await changed.wait()
            return response(
                "structured_output",
                {
                    "winner": "B",
                    "requirements_complete": True,
                    "requirements": [
                        {
                            "requirement": "value returns 3 with ENABLED",
                            "a_coverage": "not_covered",
                            "b_coverage": "covered",
                            "a_evidence": ["source.py returns 2"],
                            "b_evidence": ["source.py returns 3"],
                        }
                    ],
                    "rationale": "B returns 3 and its public check passed",
                },
                1,
            )

    def build(**kwargs):
        coder = "bash" in [tool.name for tool in kwargs["agent"].tools]
        llm = Coder(2 if not sessions else 3) if coder else Judge()
        session = original_build(**kwargs, llm=llm)
        sessions.append(session)
        return session

    monkeypatch.setattr(wiring, "build_session", build)

    async def external_change():
        await judge_entered.wait()
        if physical:
            (repo / "helper.sh").write_bytes(helper.replace(b"\n", b"\r\n"))
        else:
            git(repo, "checkout", "-q", "other")
        changed.set()

    async def direct(ctx, _args):
        candidate = await ctx.candidate_agent(
            "edit and check", label="direct", tools=evidence_tools("file_write", "bash", headless=False)
        )
        assert candidate.source_revision == original_revision
        git(repo, "checkout", "-q", "other")
        with pytest.raises(CandidateWorkspaceTrackingError):
            await ctx.adopt_candidate(candidate)
        return "revision-change-rejected"

    mutator = asyncio.create_task(external_change()) if mode == "duo" and mutate else None
    try:
        result = await OpenCollab(repo, model="scripted-local", provider="openai").workflow(
            "duo" if mode == "duo" else direct,
            {
                "goal": "value returns 3 with ENABLED",
                "candidate_evidence_dir": str((repo if physical else tmp_path) / "evidence"),
                "allow_unisolated_shell": True,
            },
            budget=100_000,
            max_steps=8,
            timeout=None,
            trace=False,
        )
        final = subprocess.run(shlex.split(command), cwd=repo, capture_output=True, text=True)
        print(
            json.dumps(
                {
                    "mode": mode,
                    "mutate": mutate,
                    "physical": physical,
                    "runtime_status": result.status,
                    "output": result.output,
                    "source": (repo / "source.py").read_text(),
                    "flags": (repo / "flags.py").read_text(),
                    "final_exit": final.returncode,
                },
                ensure_ascii=False,
            )
        )
        if mode == "direct":
            assert result.output == "revision-change-rejected"
            assert (repo / "source.py").read_text() == "def value():\n    return 1\n"
        elif mutate and physical:
            assert not result.ok
            assert "source worktree changed before candidate adoption" in result.reason
            assert (repo / "source.py").read_text() == "def value():\n    return 1\n"
            assert (repo / "helper.sh").read_bytes() == helper.replace(b"\n", b"\r\n")
        elif mutate:
            current_revision = git(repo, "rev-parse", "HEAD").strip()
            assert current_revision != original_revision
            assert current_revision == git(repo, "rev-parse", "other").strip()
            assert not result.ok
            assert "source worktree changed before candidate adoption" in result.reason
            assert (repo / "source.py").read_text() == "def value():\n    return 1\n"
            assert (repo / "flags.py").read_text() == "ENABLED = False\n"
        else:
            assert result.output["adopted"] == "B"
            assert final.returncode == 0
        if physical:
            assert (repo / ".git/index").read_bytes() == source_index
    finally:
        if mutator is not None:
            if not mutator.done():
                mutator.cancel()
            await asyncio.gather(mutator, return_exceptions=True)
