"""A caller-supplied candidate backend survives the complete public SDK path."""

from __future__ import annotations

import pytest

from opencollab import OpenCollab
from opencollab.workflows import CandidateRun


class StateCandidates:
    def __init__(self):
        self.selected = ""
        self.adoptions = []

    async def source_diff(self, exclude_paths=()):
        return self.selected

    async def diff(self):
        return self.selected

    async def changed(self):
        return bool(self.selected)

    async def changed_excluding(self, paths):
        return bool(self.selected)

    async def adopt(self, patch, preserve_paths=()):
        self.adoptions.append((patch, tuple(preserve_paths)))
        self.selected = patch


@pytest.mark.asyncio
async def test_public_workflow_uses_injected_state_backend_without_git(tmp_path):
    backend = StateCandidates()
    patch = "diff --git a/etc/service.conf b/etc/service.conf\n+enabled=true\n"

    async def choose(ctx, _inputs):
        assert await ctx.diff() == ""
        candidate = CandidateRun("B", "done", patch, (), ())
        await ctx.adopt_candidate(candidate)
        return {"diff": await ctx.diff(), "changed": await ctx.source_changed()}

    result = await OpenCollab(tmp_path).workflow(
        choose, candidate_workspace=backend, trace=False
    )

    assert result.status == "completed"
    assert result.output == {"diff": patch, "changed": True}
    assert backend.adoptions == [(patch, ())]
    assert not (tmp_path / ".git").exists()


@pytest.mark.asyncio
async def test_falsey_candidate_backend_is_preserved(tmp_path):
    class FalseyBackend(StateCandidates):
        def __bool__(self):
            return False

    backend = FalseyBackend()
    backend.selected = "real environment evidence"

    async def inspect(ctx, _inputs):
        return await ctx.diff()

    result = await OpenCollab(tmp_path).workflow(
        inspect, candidate_workspace=backend, trace=False
    )
    assert result.output == "real environment evidence"


@pytest.mark.asyncio
async def test_full_environment_adoption_retains_candidate_identity(tmp_path):
    class NamedBackend(StateCandidates):
        async def adopt_run(self, candidate, preserve_paths=()):
            self.adoptions.append((candidate.label, candidate.diff))
            self.selected = candidate.diff

    backend = NamedBackend()

    async def choose(ctx, _inputs):
        # Equal file evidence can still belong to distinct live containers.
        candidate = CandidateRun("B", "done", "same file diff", (), ())
        await ctx.adopt_candidate(candidate)
        return "B"

    result = await OpenCollab(tmp_path).workflow(
        choose, candidate_workspace=backend, trace=False
    )
    assert result.output == "B"
    assert backend.adoptions == [("B", "same file diff")]


@pytest.mark.asyncio
async def test_public_candidate_port_executes_isolated_edits_and_verification(tmp_path, monkeypatch):
    import json
    import shlex
    import subprocess
    import sys

    from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.adapters.llm.types import LLMResponse, Usage
    from opencollab.bootstrap import _workflow_runtime_session as workflow_session
    from opencollab.tools import builtin_tools

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n")
    (repo / "test_probe.py").write_text("from source import value\ndef test_probe():\n    assert value == 2\n")
    for args in (
        ("init", "-q"), ("config", "user.email", "tests@example.com"),
        ("config", "user.name", "OpenCollab Tests"), ("add", "."), ("commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    environment = LocalEnvironment(str(repo))
    backend = EnvCandidateWorkspace(environment)
    sessions = []
    original = workflow_session.build_session
    file_write, bash = builtin_tools("file_write", "bash", headless=False)

    class EvidenceBash:
        def __getattr__(self, name):
            return getattr(bash, name)

        verification_records = []
        verified_targets = ()

        async def execute_with_runtime(self, params, runtime):
            result = await bash.execute_with_runtime(params, runtime)
            self.verification_records.append({"output": result})
            if "1 passed" in result and result.startswith("Exit code: 0"):
                self.verified_targets = ("test_probe.py",)
            return result

    probe = EvidenceBash()

    class CandidateLLM:
        count = 0

        async def complete(self, messages, tools=None, **kwargs):
            self.count += 1
            calls = []
            if self.count <= 2:
                name, args = (
                    ("file_write", {"path": "source.py", "mode": "create", "content": "value = 2\n", "overwrite": True})
                    if self.count == 1 else
                    ("bash", {"command": f"{shlex.quote(sys.executable)} -m pytest -q test_probe.py"})
                )
                calls = [{"id": f"call_{self.count}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args),
                }}]
            return LLMResponse(
                content=None if calls else "finished", tool_calls=calls,
                finish_reason="tool_calls" if calls else "stop", usage=Usage(4, 2),
            )

    def build(**kwargs):
        session = original(**kwargs, llm=CandidateLLM())
        sessions.append((session, kwargs["env"]))
        return session

    monkeypatch.setattr(workflow_session, "build_session", build)

    async def choose(ctx, _inputs):
        assert await ctx.diff() == ""
        candidate = await ctx.candidate_agent("repair and verify", label="A", tools=[file_write, probe])
        assert (repo / "source.py").read_text() == "value = 1\n"
        assert await ctx.source_changed() is False
        assert candidate.verified_targets == ("test_probe.py",), probe.verification_records
        assert "1 passed" in candidate.test_records[0]["output"]
        await ctx.adopt_candidate(candidate)
        assert await ctx.source_changed() is True
        assert await ctx.source_changed(["source.py"]) is False
        return await ctx.diff()

    result = await OpenCollab(repo, environment=environment).workflow(
        choose, candidate_workspace=backend, agent_profile="single2", trace=False,
    )
    assert result.ok, result.reason
    assert "value = 2" in result.output
    assert (repo / "source.py").read_text() == "value = 2\n"
    assert sessions[0][1].workspace != str(repo)
    assert sessions[0][0].agent.find_tool("bash") is probe
