from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.tools import builtin_tools
from tests.workflows.test_candidate_adoption_recovery import _flatten, _repository, _run
from tests.workflows.test_workflow_candidate_workspace import _git


class _PublicationBarrier(LocalEnvironment):
    def __init__(self, workspace):
        super().__init__(str(workspace))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.publications = []

    async def exec_cmd(self, command, timeout=120, **kwargs):
        publication = " apply --binary " in command and ".candidate-adopt-" in command
        if publication:
            self.entered.set()
            await self.release.wait()
        result = await super().exec_cmd(command, timeout, **kwargs)
        if publication:
            self.publications.append(result.returncode)
        return result


class _WriterModel:
    def __init__(self, tool, path):
        self.tool, self.path, self.calls = tool, path, 0
        self.requested = asyncio.Event()

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.requested.set()
            arguments = {"path": self.path}
            if self.tool == "file_write":
                arguments.update(mode="create", content="VALUE = 100\n")
            else:
                arguments.update(mode="line_replace", start_line=1, end_line=1, new_str="VALUE = 100")
            return LLMResponse(tool_calls=[{
                "id": "shared-edit", "type": "function", "function": {
                    "name": self.tool, "arguments": json.dumps(arguments),
                },
            }], usage=Usage(10, 5), finish_reason="tool_calls")
        assert any("100" in str(message) for message in messages)
        return LLMResponse(content="Shared edit complete", usage=Usage(10, 5), finish_reason="stop")


@pytest.mark.parametrize("tool", ["file_write", "apply_patch"])
@pytest.mark.parametrize("relation,failure", [
    ("target", False), ("target", True), ("descendant", True),
    ("unrelated", False), ("unrelated", True),
])
async def test_sdk_shared_agent_owns_mutations_through_candidate_publication(
    tmp_path, tool, relation, failure,
):
    repo = _repository(tmp_path, cache=relation == "descendant")
    if failure and relation != "descendant":
        (repo / "blocked").mkdir()
        (repo / "blocked/entry.py").write_text("VALUE = 1\n")
        _git(repo, "add", "blocked/entry.py")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare directory replacement")
        (repo / "blocked/__pycache__").mkdir()
        (repo / "blocked/__pycache__/retained.pyc").write_bytes(b"retained ignored bytes")
    environment = _PublicationBarrier(repo)
    path = "other.py" if relation == "unrelated" else "settings/defaults.py"
    model = _WriterModel(tool, path)
    index = (repo / ".git/index").read_bytes()

    async def edit(child, _):
        folder = Path(child.workspace_root)
        if relation == "descendant":
            _flatten(folder)
        else:
            (folder / "settings/defaults.py").write_text("VALUE = 42\n")
            if failure:
                (folder / "blocked/entry.py").unlink()
                (folder / "blocked").rmdir()
                (folder / "blocked").write_text("replacement\n")
        return "prepared"

    async def flow(ctx, _):
        candidate = await ctx.candidate_workflow(edit, {}, label="forty-two")
        publication = asyncio.create_task(ctx.adopt_candidate(candidate))
        writer = None
        try:
            await asyncio.wait_for(environment.entered.wait(), 10)
            writer = asyncio.create_task(ctx.agent_run(
                "Set the shared value to 100", tools=list(builtin_tools(tool, headless=False)),
                isolation="shared", budget=2000, system_prompt="Use the requested file tool",
            ))
            await asyncio.wait_for(model.requested.wait(), 10)
            if relation == "unrelated":
                result = await asyncio.wait_for(asyncio.shield(writer), 10)
                assert result.status == "completed"
                assert (repo / path).read_text() == "VALUE = 100\n"
            else:
                await asyncio.sleep(0.05)
                assert not writer.done()
                assert (repo / path).read_text() == "VALUE = 1\n"
            environment.release.set()
            if failure:
                with pytest.raises(RuntimeError, match="Directory not empty|Is a directory"):
                    await publication
            else:
                await publication
            result = await writer
            assert result.status == "completed"
            assert (repo / path).read_text() == "VALUE = 100\n"
            assert len(environment.publications) == 1
            assert (environment.publications[0] != 0) is failure
            return "retained"
        finally:
            environment.release.set()
            await asyncio.gather(publication, *([writer] if writer is not None else []), return_exceptions=True)

    try:
        result = await OpenCollab(repo, environment=environment, model="local-scripted").workflow(
            flow, llm=model, trace=False, timeout=40, budget=10000, concurrency=2,
            limit_mode="explicit", agent_profile="single2",
        )
        assert result.ok, result.reason
        assert result.output == "retained"
        assert _run(repo) == ("100\n" if relation != "unrelated" else "1\n" if failure else "42\n")
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment.revoked
    finally:
        environment.release.set()
        await environment.cleanup()
