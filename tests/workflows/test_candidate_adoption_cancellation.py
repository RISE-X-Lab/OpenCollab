from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from opencollab.adapters._candidate_increment import _apply_increment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_candidates import CandidateRun
from tests.workflows.test_candidate_adoption_recovery import _repository
from tests.workflows.test_workflow_candidate_workspace import _git


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_candidate_adoption_finishes_before_propagating_repeated_cancellation(
    tmp_path, modern, failure,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    selected = CandidateRun("selected", "tested", "complete patch", (), ())
    calls = []

    async def deliver(candidate, preserve_paths):
        calls.append((candidate, preserve_paths))
        (tmp_path / "app.py").write_text("from dependency import VALUE\nprint(VALUE)\n")
        entered.set()
        await release.wait()
        (tmp_path / "dependency.py").write_text("VALUE = 42\n")
        completed.set()
        if failure:
            raise OSError("delivery cleanup failed")

    backend = SimpleNamespace(**{"adopt_run" if modern else "adopt": deliver})
    context = WorkflowContext(object(), candidate_workspace=backend)
    operation = asyncio.create_task(context.adopt_candidate(selected, preserve_paths=("tests",)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        for _ in range(2):
            operation.cancel()
            await asyncio.sleep(0)
        assert not operation.done()
        assert not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(operation, 10)
        assert completed.is_set()
        assert calls == [(selected if modern else selected.diff, ("tests",))]
        if failure and sys.version_info >= (3, 11):
            assert any("delivery cleanup failed" in note for note in getattr(cancelled.value, "__notes__", ()))
        executed = subprocess.run(
            [sys.executable, "-B", "app.py"], cwd=tmp_path, capture_output=True, check=True,
        )
        assert executed.stdout == b"42\n"
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("modern", [False, True])
async def test_candidate_adoption_preserves_uncancelled_backend_failure(modern):
    failure = OSError("delivery failed")

    async def deliver(candidate, preserve_paths):
        raise failure

    backend = SimpleNamespace(**{"adopt_run" if modern else "adopt": deliver})
    context = WorkflowContext(object(), candidate_workspace=backend)
    selected = CandidateRun("selected", "tested", "complete patch", (), ())
    with pytest.raises(OSError) as raised:
        await context.adopt_candidate(selected)
    assert raised.value is failure


async def test_backend_cancellation_cleans_recovery_directory_created_before_receipt(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    original = (repo / "other.py").read_text()
    (repo / "other.py").write_text("VALUE = 42\n")
    patch = _git(repo, "diff", "--binary", "--", "other.py")
    _git(repo, "restore", "--worktree", "--", "other.py")
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    allocated, release_receipt = asyncio.Event(), asyncio.Event()

    async def hold_recovery_directory_receipt(command, *args, **kwargs):
        result = await execute(command, *args, **kwargs)
        if command.startswith("mktemp -d ") and "candidate-recovery-" in command:
            allocated.set()
            await release_receipt.wait()
        return result

    monkeypatch.setattr(environment, "exec_cmd", hold_recovery_directory_receipt)
    task = asyncio.create_task(_apply_increment(environment, str(repo), patch))
    try:
        await asyncio.wait_for(allocated.wait(), 5)
        assert list((repo / ".git").glob("candidate-recovery-*"))
        task.cancel()
        await asyncio.sleep(0)
        release_receipt.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not list((repo / ".git").glob("candidate-recovery-*"))
        assert (repo / "other.py").read_text() == original
    finally:
        release_receipt.set()
        await asyncio.gather(task, return_exceptions=True)
        await environment.cleanup()
