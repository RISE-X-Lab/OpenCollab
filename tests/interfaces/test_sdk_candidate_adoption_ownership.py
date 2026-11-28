from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys

import pytest

from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.sdk import OpenCollab
from opencollab.tools import builtin_tools


@pytest.mark.parametrize("join", [False, True], ids=["plain-gather", "explicit-join"])
@pytest.mark.parametrize("paused_phase", ["revision", "publication"])
async def test_sdk_failure_waits_for_in_flight_candidate_publication(tmp_path, join, paused_phase):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "app.py"
    original = "def subtotal(price, count):\n    return 0\n"
    proposed = "def subtotal(price, count):\n    return price * count\n"
    source.write_text(original)
    (workspace / "test_app.py").write_text(
        "from app import subtotal\n\n"
        "def test_invoice():\n"
        "    assert subtotal(5, 4) == 20\n"
    )
    for args in (
        ("init", "-q"), ("add", "app.py", "test_app.py"),
        ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Prepare invoice fixture"),
    ):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    starting, publishing, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sibling_failure = ValueError("Optional notes are unavailable")
    owner = LocalEnvironment(str(workspace))
    adoption_tasks = []
    verified = []
    candidate_ready = False

    class SlowPublication(EnvCandidateWorkspace):
        async def pause(self):
            starting.set()
            await publishing.wait()

        async def source_revision(self):
            if candidate_ready and paused_phase == "revision" and not starting.is_set():
                await self.pause()
            return await super().source_revision()

        async def adopt_run(self, candidate, preserve_paths=()):
            if paused_phase == "publication":
                await self.pause()
            await super().adopt_run(candidate, preserve_paths)

    async def candidate_work(child, _args):
        write, bash = builtin_tools("file_write", "bash", headless=False)
        await child.execute_verification(write, {"path": "app.py", "mode": "create", "content": proposed})
        output = await child.execute_verification(bash, {
            "command": f"{shlex.quote(sys.executable)} -B -m pytest -q -rA -p no:cacheprovider test_app.py",
        })
        assert "1 passed" in output and "test_app.py::test_invoice" in output
        verified.append(output)
        return "candidate ready"

    async def workflow(ctx, inputs):
        nonlocal candidate_ready
        candidate = await ctx.candidate_workflow(candidate_work, inputs, label="invoice", budget=4000)
        assert candidate.output == "candidate ready"
        assert "+    return price * count" in candidate.diff
        candidate_ready = True

        async def check_notes():
            await starting.wait()
            failed.set()
            raise sibling_failure

        adopting = asyncio.create_task(ctx.adopt_candidate(candidate))
        adoption_tasks.append(adopting)
        if join:
            _published, error = await asyncio.gather(adopting, check_notes(), return_exceptions=True)
            raise error
        await asyncio.gather(adopting, check_notes())

    artifacts = tmp_path / "artifacts"
    client = OpenCollab(workspace, model="offline", environment=owner)
    job = asyncio.create_task(client.workflow(
        workflow, budget=10_000, limit_mode="explicit", timeout=15, cleanup_timeout=2,
        trace=False, artifacts=artifacts, candidate_workspace=SlowPublication(owner),
    ))
    try:
        await asyncio.wait_for(failed.wait(), 10)
        await asyncio.wait({job}, timeout=0.2)
        assert not job.done()
        assert not (artifacts / "workflow.json").exists()
        assert source.read_text() == original
        publishing.set()
        result = await asyncio.wait_for(job, 10)
        assert adoption_tasks[0].done()
        assert adoption_tasks[0].result() is None
        assert result.status == "failed" and result.error is sibling_failure
        assert len(verified) == 1 and source.read_text() == proposed
        manifest = json.loads((artifacts / "workflow.json").read_text())
        assert manifest["status"] == "failed" and manifest["evidence_complete"] is True
        assert not owner.revoked
    finally:
        publishing.set()
        await asyncio.gather(job, *adoption_tasks, return_exceptions=True)
        await owner.cleanup()
