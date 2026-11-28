from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.sdk import OpenCollab
from opencollab.tools import builtin_tools


@pytest.mark.parametrize("scene", ["success", "sibling", "joined", "cancel"])
async def test_local_verification_settles_before_workflow_returns(tmp_path, scene):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = workspace / "verifier.started"
    proceed = workspace / "continue.check"
    verified = workspace / "verified.txt"
    (workspace / "verify_report.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "Path('verifier.started').write_text('started')\n"
        "while not Path('continue.check').exists():\n"
        "    time.sleep(.02)\n"
        "Path('verified.txt').write_text('report verified\\n')\n"
        "print('VERIFIED')\n"
    )
    (workspace / "verify_reuse.py").write_text("print('REUSABLE')\n")
    python = shlex.quote(sys.executable)
    sibling_failed = asyncio.Event()
    subject_error = ValueError("Required deployment date is missing")
    verification_tasks = []

    async def wait_started():
        while not started.is_file():
            await asyncio.sleep(0.005)

    async def run_verification(ctx, source):
        bash, = builtin_tools("bash", headless=False)
        return await ctx.execute_verification(bash, {"command": f"{python} -B {source}"})

    async def child(ctx, _args):
        async def check_inputs():
            await wait_started()
            sibling_failed.set()
            raise subject_error

        if scene == "success":
            return await run_verification(ctx, "verify_report.py")
        verifier = asyncio.create_task(run_verification(ctx, "verify_report.py"))
        verification_tasks.append(verifier)
        if scene == "cancel":
            await asyncio.Future()
        elif scene == "joined":
            _report, failure = await asyncio.gather(verifier, check_inputs(), return_exceptions=True)
            raise failure
        else:
            await asyncio.gather(verifier, check_inputs())

    async def parent(ctx, args):
        return await child(ctx, args)

    async def reuse(ctx, _args):
        return await run_verification(ctx, "verify_reuse.py")

    environment = LocalEnvironment(str(workspace))
    client = OpenCollab(workspace, model="offline-model", environment=environment)
    artifacts = tmp_path / "artifacts"

    async def run_workflow():
        try:
            return await client.workflow(
                parent, budget=10_000, limit_mode="explicit", timeout=10, cleanup_timeout=1,
                trace=False, artifacts=artifacts,
            )
        except asyncio.CancelledError as error:
            return error

    running = asyncio.create_task(run_workflow())
    try:
        await asyncio.wait_for(wait_started(), 5)
        if scene in {"sibling", "joined"}:
            await asyncio.wait_for(sibling_failed.wait(), 5)
        if scene == "cancel":
            running.cancel("cancel parent while verifying")
            result = await asyncio.wait_for(running, 5)
            assert isinstance(result, asyncio.CancelledError)
            assert str(result) == "cancel parent while verifying"
            verifier, = verification_tasks
            assert verifier.cancelled()
            assert not verified.exists()
            proceed.write_text("proceed")
        else:
            await asyncio.wait({running}, timeout=0.2)
            assert not running.done()
            assert not (artifacts / "workflow.json").exists()
            assert not verified.exists()
            proceed.write_text("proceed")
            result = await asyncio.wait_for(running, 5)
            if scene == "success":
                assert result.ok and "VERIFIED" in result.output
            else:
                assert result.status == "failed" and result.error is subject_error
                verifier, = verification_tasks
                assert verifier.done() and "VERIFIED" in verifier.result()
            assert verified.read_text() == "report verified\n"

        manifest = json.loads((artifacts / "workflow.json").read_text())
        expected = {"success": "completed", "cancel": "stopped", "sibling": "failed", "joined": "failed"}
        assert manifest["status"] == expected[scene]
        assert manifest["evidence_complete"] is True
        assert not environment.revoked
        reused = await client.workflow(reuse, trace=False, timeout=5)
        assert reused.ok and "REUSABLE" in reused.output
        assert verified.exists() is (scene != "cancel")
    finally:
        proceed.touch()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, *verification_tasks, return_exceptions=True)
        await environment.cleanup()
