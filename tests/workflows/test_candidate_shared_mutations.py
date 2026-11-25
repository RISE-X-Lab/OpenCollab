from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError
from opencollab.tools import builtin_tools
from tests.workflows.test_candidate_adoption_recovery import (
    _detached_files,
    _flatten,
    _partial_publication,
    _repository,
    _run,
)
from tests.workflows.test_workflow_candidate_workspace import _git


class _PublicationBarrier(LocalEnvironment):
    def __init__(self, workspace, *, after_write=False):
        super().__init__(str(workspace))
        self.after_write = after_write
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.publications = []

    async def exec_cmd(self, command, timeout=120, **kwargs):
        publication = " apply --binary " in command and ".candidate-adopt-" in command
        if publication and not self.after_write:
            self.entered.set()
            await self.release.wait()
        result = await super().exec_cmd(command, timeout, **kwargs)
        if publication:
            self.publications.append(result.returncode)
            if self.after_write:
                self.entered.set()
                await self.release.wait()
        return result


class _WriterModel:
    def __init__(self, tool, path, *, command=None):
        self.tool, self.path, self.calls = tool, path, 0
        self.command = command
        self.requested = asyncio.Event()

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.requested.set()
            arguments = {"path": self.path}
            if self.tool == "bash":
                arguments = {"command": self.command or f"printf '%s\\n' 'shared replacement' > {self.path}"}
            elif self.tool == "file_write":
                arguments.update(mode="create", content="VALUE = 100\n")
            else:
                arguments.update(mode="line_replace", start_line=1, end_line=1, new_str="VALUE = 100")
            return LLMResponse(tool_calls=[{
                "id": "shared-edit", "type": "function", "function": {
                    "name": self.tool, "arguments": json.dumps(arguments),
                },
            }], usage=Usage(10, 5), finish_reason="tool_calls")
        evidence = "Exit code: 0" if self.tool == "bash" else "100"
        assert any(evidence in str(message) for message in messages)
        return LLMResponse(content="Shared edit complete", usage=Usage(10, 5), finish_reason="stop")


def _assert_flatten_recovery(repo: Path, loader: bytes) -> None:
    assert (repo / "loader.py").read_bytes() == loader
    assert (repo / "draft.txt").read_bytes() == b"untracked user draft\n"
    assert _detached_files(repo / ".git") == (
        {
            repo / "loader.py": loader,
            repo / "draft.txt": b"untracked user draft\n",
            repo / "settings/defaults.py": b"VALUE = 1\n",
        },
        {
            repo / "loader.py": (
                b'from pathlib import Path\ndef value():\n    return int(Path("settings").read_text())\n'
            ),
            repo / "draft.txt": b"candidate draft\n",
        },
    )


@pytest.mark.parametrize("tool", ["file_write", "apply_patch"])
@pytest.mark.parametrize("relation,failure", [
    ("target", False), ("target", True), ("descendant", True),
    ("unrelated", False), ("unrelated", True), ("null-attributes-sibling", False),
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
    path = {
        "unrelated": "other.py",
        "null-attributes-sibling": "settings/independent.py",
    }.get(relation, "settings/defaults.py")
    if relation == "null-attributes-sibling":
        _git(repo, "config", "--local", "core.attributesFile", "/dev/null")
        (repo / path).write_text("VALUE = 1\n")
    independent = relation in {"unrelated", "null-attributes-sibling"}
    model = _WriterModel(tool, path)
    loader = (repo / "loader.py").read_bytes()
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
            if independent:
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
        assert _run(repo) == ("100\n" if not independent else "1\n" if failure else "42\n")
        assert (repo / ".git/index").read_bytes() == index
        if not failure:
            assert not list((repo / ".git").glob("candidate-recovery-*"))
        elif relation == "descendant":
            _assert_flatten_recovery(repo, loader)
        else:
            blocked = repo / "blocked/entry.py"
            assert blocked.read_bytes() == b"VALUE = 1\n"
            assert _detached_files(repo / ".git") == (
                {blocked: b"VALUE = 1\n", repo / "settings/defaults.py": b"VALUE = 1\n"},
                {},
            )
        assert not environment.revoked
    finally:
        environment.release.set()
        await environment.cleanup()


@pytest.mark.parametrize("after_write,path", [
    (True, "loader.py"), (True, "settings/ignored.cache"), (True, "settings/notes.txt"), (True, "separate.txt"),
    (False, "settings/notes.txt"),
])
async def test_sdk_shared_bash_preserves_writes_around_git_publication_failure(tmp_path, after_write, path):
    repo = _repository(tmp_path)
    if after_write:
        with (repo / ".gitignore").open("a") as ignore:
            ignore.write("settings/ignored.cache\n")
        (repo / "settings/ignored.cache").write_text("runtime cache\n")
    loader = (repo / "loader.py").read_bytes()
    index = (repo / ".git/index").read_bytes()
    environment = _PublicationBarrier(repo, after_write=after_write)
    overlap = path == "loader.py"

    async def edit(child, _):
        _flatten(Path(child.workspace_root))

    async def flow(ctx, _):
        candidate = await ctx.candidate_workflow(edit, {}, label="flatten")
        publication = asyncio.create_task(ctx.adopt_candidate(candidate))
        try:
            await asyncio.wait_for(environment.entered.wait(), 10)
            assert ((repo / "loader.py").read_bytes() != loader) is after_write
            writer = await asyncio.wait_for(ctx.agent_run(
                "Save this file in the shared workspace", tools=list(builtin_tools("bash", headless=False)),
                isolation="shared", budget=2000, system_prompt="Use the requested Bash operation",
            ), 10)
            assert writer.status == "completed"
            assert (repo / path).read_text() == "shared replacement\n"
            environment.release.set()
            error = CandidateAdoptionRecoveryError if overlap else RuntimeError
            detail = "concurrent change at .*/loader.py" if overlap else "Directory not empty|Is a directory"
            with pytest.raises(error, match=detail):
                await publication
        finally:
            environment.release.set()
            await asyncio.gather(publication, return_exceptions=True)

    try:
        result = await OpenCollab(repo, environment=environment, model="local-scripted").workflow(
            flow, llm=_WriterModel("bash", path), trace=False, timeout=30, budget=5000,
            concurrency=2, limit_mode="explicit", agent_profile="single2",
        )
        assert result.ok, result.reason
        assert environment.publications == [128]
        assert (repo / path).read_text() == "shared replacement\n"
        assert (repo / "settings/defaults.py").read_text() == "VALUE = 1\n"
        assert (repo / "draft.txt").read_text() == "untracked user draft\n"
        assert (repo / ".git/index").read_bytes() == index
        if overlap:
            backup, = (repo / ".git").glob("candidate-recovery-*")
            for record in backup.glob("*.path"):
                if Path(record.read_text()).name == "loader.py":
                    assert record.with_suffix("").read_bytes() == loader
                    break
            else:
                pytest.fail("recovery did not retain the original loader")
        else:
            _assert_flatten_recovery(repo, loader)
    finally:
        await environment.cleanup()


class _ParkingBarrier(_PublicationBarrier):
    def __init__(self, workspace):
        super().__init__(workspace, after_write=True)
        self.parking = asyncio.Event()
        self.allow_parking = asyncio.Event()
        self.parked = asyncio.Event()
        self.return_recovery = asyncio.Event()

    async def exec_cmd(self, command, timeout=120, **kwargs):
        recovery = command.startswith("failed=0;")
        if recovery:
            self.parking.set()
            await self.allow_parking.wait()
        result = await super().exec_cmd(command, timeout, **kwargs)
        if recovery:
            self.parked.set()
            await self.return_recovery.wait()
        return result


@pytest.mark.parametrize("finished_inside_recovery", [False, True])
async def test_sdk_shared_bash_retains_writes_through_an_open_file(tmp_path, finished_inside_recovery):
    repo, candidate = _partial_publication(tmp_path)
    target = repo / "record.txt"
    original = target.read_bytes()
    index = (repo / ".git/index").read_bytes()
    environment = _ParkingBarrier(repo)
    ready = repo / "writer.ready"
    release = repo / "writer.release"
    appended = b"# shared shell bytes\n"
    command = shlex.join((sys.executable, "-c", f"""
import os
import time
from pathlib import Path

with Path({target.name!r}).open("ab") as stream:
    Path({ready.name!r}).touch()
    deadline = time.monotonic() + 10
    while not Path({release.name!r}).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("waiting for recovery")
        time.sleep(0.01)
    stream.write({appended!r})
    stream.flush()
    os.fsync(stream.fileno())
"""))

    async def wait_for_writer():
        while not ready.exists():
            await asyncio.sleep(0.01)

    async def flow(ctx, _):
        publication = asyncio.create_task(ctx.adopt_candidate(candidate))
        writer = None
        try:
            await asyncio.wait_for(environment.entered.wait(), 10)
            published = target.read_bytes()
            assert published != original
            if finished_inside_recovery:
                environment.release.set()
                await asyncio.wait_for(environment.parking.wait(), 10)
            writer = asyncio.create_task(ctx.agent_run(
                "Save the shared Bash result", tools=list(builtin_tools("bash", headless=False)),
                isolation="shared", budget=2000, system_prompt="Run the shell command",
            ))
            await asyncio.wait_for(wait_for_writer(), 10)
            environment.release.set()
            environment.allow_parking.set()
            await asyncio.wait_for(environment.parked.wait(), 10)
            if finished_inside_recovery:
                release.touch()
                assert (await asyncio.wait_for(writer, 10)).status == "completed"
            environment.return_recovery.set()
            with pytest.raises(CandidateAdoptionRecoveryError, match="concurrent shell command.*retained"):
                await asyncio.wait_for(asyncio.shield(publication), 10)
            release.touch()
            assert (await asyncio.wait_for(writer, 10)).status == "completed"
            return published
        finally:
            environment.release.set()
            environment.allow_parking.set()
            environment.return_recovery.set()
            release.touch()
            await asyncio.gather(publication, *([writer] if writer else []), return_exceptions=True)

    try:
        result = await OpenCollab(repo, environment=environment, model="local-scripted").workflow(
            flow, llm=_WriterModel("bash", target.name, command=command), trace=False, timeout=40,
            budget=5000, concurrency=2, limit_mode="explicit", agent_profile="single2",
        )
        assert result.ok, result.reason
        assert environment.publications == [128]
        assert target.read_bytes() == original
        assert (repo / "settings/notes.txt").read_text() == "existing settings\n"
        assert (repo / ".git/index").read_bytes() == index
        retained, = (repo / ".git").glob("candidate-recovery-*")
        record, = (record for record in retained.glob("observed-*/path") if record.read_text() == str(target))
        assert record.with_name("entry").read_bytes() == result.output + appended
        assert not environment.revoked
    finally:
        await environment.cleanup()
