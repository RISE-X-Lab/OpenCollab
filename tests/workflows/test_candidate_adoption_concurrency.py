from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from tests.workflows.test_candidate_adoption_recovery import _repository, _run

_ADOPTER = ContextVar("adopter", default="setup")


class _OrderedEnvironment(LocalEnvironment):
    def __init__(self, root, *, remote):
        super().__init__(str(root))
        self.local_filesystem = not remote
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()
        self.publications = []
        self.checks = []
        self.remote_lock = asyncio.Lock()
        self.remote_lock_held = False
        self.lock_paths = []

    @asynccontextmanager
    async def file_write_lock(self, path):
        assert Path(path).parent == Path(self.workspace)
        assert not Path(path).exists()
        self.lock_paths.append(path)
        async with self.remote_lock:
            self.remote_lock_held = True
            try:
                yield path
            finally:
                self.remote_lock_held = False

    async def exec_cmd(self, command, timeout=120.0, **kwargs):
        if " apply --check --binary " in command or " apply --binary " in command:
            if not self.local_filesystem:
                assert self.remote_lock_held
            label = _ADOPTER.get()
            if " apply --check --binary " in command:
                self.checks.append(label)
            else:
                self.publications.append(label)
                if label == "A":
                    self.first_entered.set()
                    await self.release_first.wait()
        return await super().exec_cmd(command, timeout, **kwargs)


class _QueuedCandidates(EnvCandidateWorkspace):
    def __init__(self, environment):
        super().__init__(environment)
        self.second_requested = asyncio.Event()

    async def adopt_run(self, candidate, preserve_paths=()):
        if candidate.label == "B":
            self.second_requested.set()
        await super().adopt_run(candidate, preserve_paths)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("entry,cancel,overlap", [
    ("sdk", False, False), ("sdk", False, True),
    ("direct", True, False), ("sdk", True, False),
])
async def test_candidate_publication_serializes_snapshots_and_queued_cancellation(
    tmp_path, remote, entry, cancel, overlap,
):
    repo = _repository(tmp_path)
    index = (repo / ".git/index").read_bytes()
    environment = _OrderedEnvironment(repo, remote=remote)
    backend = _QueuedCandidates(environment)

    async def edit(child, inputs):
        (Path(child.workspace_root) / inputs["path"]).write_text(inputs["contents"])
        return "prepared"

    async def flow(ctx, inputs):
        first = await ctx.candidate_workflow(edit, {"path": "app.py", "contents": "print(42)\n"}, label="A")
        second_path = "app.py" if overlap else "other.py"
        second = await ctx.candidate_workflow(
            edit, {"path": second_path, "contents": "print(99)\n"}, label="B",
        )

        async def adopt(candidate, selected_entry):
            token = _ADOPTER.set(candidate.label)
            try:
                if selected_entry == "direct":
                    await backend.adopt_run(candidate)
                else:
                    await ctx.adopt_candidate(candidate)
            finally:
                _ADOPTER.reset(token)

        first_task = asyncio.create_task(adopt(first, "sdk"))
        second_task = None
        try:
            await asyncio.wait_for(environment.first_entered.wait(), 10)
            second_task = asyncio.create_task(adopt(second, entry))
            await asyncio.wait_for(backend.second_requested.wait(), 10)
            await asyncio.sleep(0.05)
            assert not second_task.done()
            assert environment.checks == environment.publications == ["A"]
            if cancel:
                second_task.cancel()
                await asyncio.sleep(0)
                second_task.cancel()
            environment.release_first.set()
            await first_task
            assert _run(repo) == "42\n"
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await second_task
            elif overlap:
                with pytest.raises(RuntimeError, match="patch does not apply"):
                    await second_task
            else:
                await second_task
            assert _run(repo) == "42\n"
            expected_second = "VALUE = 1\n" if overlap or cancel and entry == "direct" else "print(99)\n"
            assert (repo / "other.py").read_text() == expected_second
            assert environment.publications == (["A"] if overlap or cancel and entry == "direct" else ["A", "B"])
            return "serialized"
        finally:
            environment.release_first.set()
            await asyncio.gather(first_task, *(task for task in (second_task,) if task is not None),
                                 return_exceptions=True)

    try:
        result = await OpenCollab(repo, environment=environment).workflow(
            flow, candidate_workspace=backend, trace=False, timeout=30,
        )
        assert result.ok, result.reason
        assert result.output == "serialized"
        assert (repo / ".git/index").read_bytes() == index
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert not environment.revoked
        if remote:
            assert environment.lock_paths
    finally:
        await environment.cleanup()
