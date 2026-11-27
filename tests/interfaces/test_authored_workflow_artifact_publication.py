from __future__ import annotations

import asyncio
import errno
import json
import sys
from pathlib import Path

import pytest

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import authored_workflow, container
from opencollab.sdk import OpenCollab


def call(name, arguments):
    return {"id": f"call-{name}-{arguments.get('action', 'write')}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.parametrize("scene", ["settled", "wait", "result-budget", "parent-cancel", "export-fails"])
async def test_authored_terminal_acknowledgement_references_persistent_artifacts(tmp_path, monkeypatch, scene):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: writer\nroles:\n  writer:\n    prompt: Writer\n"
        "    tools: [file_write, bash, manage_workflow]\n"
    )
    source = (
        "from pathlib import Path\n\n"
        "async def main(ctx, args):\n"
        "    text = 'ready for review\\n'\n"
        "    Path('report.txt').write_text(text)\n"
        "    return {'status': 'ready', 'bytes': len(text)}\n"
    )
    copying, release, cancelled, parent_admitted = [asyncio.Event() for _ in range(4)]
    seen = {}
    copy_artifact = authored_workflow._copy_artifact_to_host

    async def copy(environment, source, target):
        if source.endswith("/workflow.json"):
            copying.set()
            await release.wait()
            if scene == "export-fails":
                raise OSError(errno.EIO, "child manifest transfer failed")
        await copy_artifact(environment, source, target)

    monkeypatch.setattr(authored_workflow, "_copy_artifact_to_host", copy)

    class Parent:
        def __init__(self, **kwargs):
            pass

        def context_window(self):
            return 200_000

        async def close(self):
            pass

        def estimate_request_tokens(self, messages, tools, **kwargs):
            replies = sum(message["role"] == "tool" for message in messages)
            return 9_500 if scene == "result-budget" and replies == 3 else 100

        async def complete(self, messages, **kwargs):
            replies = [message for message in messages if message["role"] == "tool"]
            match len(replies):
                case 0:
                    action = call("file_write", {"path": "authored.py", "mode": "create", "content": source})
                case 1:
                    action = call("manage_workflow", {"action": "launch", "source": "authored.py",
                                                     "python": sys.executable, "budget": 1_000})
                case 2:
                    launched = json.loads(replies[-1]["content"])
                    seen["id"] = launched["id"]
                    await asyncio.wait_for(copying.wait(), 5)
                    if scene == "parent-cancel":
                        try:
                            await asyncio.Future()
                        except asyncio.CancelledError:
                            cancelled.set()
                            raise
                    if scene == "settled":
                        release.set()
                    wait = {"action": "wait", "id": seen["id"]}
                    if scene == "result-budget":
                        wait["action"] = "result"
                    elif scene != "settled":
                        wait["timeout"] = 0.01
                    action = call("manage_workflow", wait)
                case 3:
                    seen["first"] = json.loads(replies[-1]["content"])
                    parent_admitted.set()
                    release.set()
                    action = call("manage_workflow", {"action": "wait", "id": seen["id"], "timeout": 5})
                case _:
                    seen["settled"] = json.loads(replies[-1]["content"])
                    return LLMResponse(content="Collected the authored report.", usage=Usage(3, 2))
            return LLMResponse(tool_calls=[action], usage=Usage(3, 2), finish_reason="tool_calls")

    monkeypatch.setattr(container, "LLMClient", Parent)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)

    async def run_team():
        try:
            return await OpenCollab(workspace, model="offline-model", config={"budget": 10_000}).team(
                "Create and collect a local report.", config=team, artifacts=tmp_path / "artifacts",
                trace=False, use_worktrees=False, allow_unisolated_shell=True, timeout=15,
            )
        except asyncio.CancelledError as error:
            return error

    job = asyncio.create_task(run_team())
    try:
        if scene == "parent-cancel":
            await asyncio.wait_for(copying.wait(), 5)
            job.cancel("stop parent during child export")
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not job.done()
            release.set()
        elif scene == "result-budget":
            await asyncio.wait_for(parent_admitted.wait(), 5)
        result = await job
    finally:
        release.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    transient = workspace / ".opencollab" / "child-runs" / seen["id"]
    assert (workspace / "report.txt").read_text() == "ready for review\n"
    if scene == "parent-cancel":
        assert isinstance(result, asyncio.CancelledError)
        assert str(result) == "stop parent during child export"
        retained, = (tmp_path / "artifacts").glob("child-*/result.json")
        saved = json.loads(retained.read_text())
        assert saved["status"] == "completed"
        assert saved["result"]["output"] == {"status": "ready", "bytes": 17}
        assert saved["artifacts"] == str(retained.parent)
        assert not transient.exists()
        return

    first, settled = seen["first"], seen["settled"]
    assert first["id"] == settled["id"]
    if scene == "export-fails":
        assert first["status"] == "running" and first["result"] is None
        assert settled["status"] == "failed" and settled["result"]["output"] is None
        assert "child manifest transfer failed" in settled["result"]["reason"]
        assert result.status == "failed" and result.tokens == 25
        assert transient.exists()
        return

    assert result.ok, (result.reason, result.error)
    assert result.output == "Collected the authored report."
    assert result.tokens == 25
    assert settled["status"] == "completed"
    assert settled["result"]["output"] == {"status": "ready", "bytes": 17}
    assert Path(settled["artifacts"], "workflow.json").is_file()
    assert json.loads(Path(settled["artifacts"], "result.json").read_text()) == settled
    assert not transient.exists()
    if scene in {"wait", "result-budget"}:
        assert first["status"] == "running" and first["result"] is None
    else:
        assert first == settled
