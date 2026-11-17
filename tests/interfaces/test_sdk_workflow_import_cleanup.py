"""Loaded workflows retain relative imports through owned tool cleanup."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from opencollab import OpenCollab, RunError
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import _workflow_runtime_discovery as discovery


def _import_state():
    return (
        {name for name in sys.modules if name.startswith("_opencollab_workflow_")},
        dict(discovery._WORKFLOW_IMPORT_FINDER._package_roots),
    )


class ResourceModel:
    def context_window(self):
        return 200_000

    async def complete(self, messages, **kwargs):
        return LLMResponse(tool_calls=[{
            "id": "resource", "type": "function", "function": {
                "name": "resource", "arguments": json.dumps({}),
            },
        }], usage=Usage(input_tokens=5, output_tokens=5), finish_reason="tool_calls")


def _loaded_workflow(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "_cleanup.py").write_text(
        "def close_resource(events):\n    events['closed'].set()\n",
        encoding="utf-8",
    )
    (directory / "resource.py").write_text('''import asyncio
from opencollab import workflow

class Resource:
    name = "resource"
    description = "Hold a resource until cancellation."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, events):
        self.events = events

    def to_openai_schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}

    async def execute_with_runtime(self, args, runtime):
        self.events["entered"].set()
        try:
            await asyncio.Event().wait()
        finally:
            self.events["cleanup_started"].set()
            while not self.events["release"].is_set():
                try:
                    await self.events["release"].wait()
                except asyncio.CancelledError:
                    self.events["cancelled_again"].set()
            from ._cleanup import close_resource
            close_resource(self.events)

@workflow
async def resource_cleanup(ctx, args):
    events = args["events"]
    events["context"] = ctx
    timeout = 0.05 if args["mode"] == "timeout" else None
    operation = asyncio.create_task(ctx.agent("Hold the resource", tools=[Resource(events)], timeout=timeout))
    await events["entered"].wait()
    if args["mode"] == "recover":
        operation.cancel()
    try:
        await operation
    except asyncio.CancelledError:
        if args["mode"] != "recover":
            raise
    events["returned"].set()
    return "recovered"
''', encoding="utf-8")
    return discovery.discover_workflows(str(directory)).get("resource_cleanup")


def _events():
    return {name: asyncio.Event() for name in (
        "entered", "cleanup_started", "release", "closed", "returned", "cancelled_again",
    )}


@pytest.mark.parametrize("mode", ["recover", "timeout", "caller", "repeated"])
async def test_loaded_workflow_keeps_imports_until_cancelled_tool_closes(tmp_path, mode):
    before = _import_state()
    spec = _loaded_workflow(tmp_path)
    events = _events()
    running = asyncio.create_task(OpenCollab(tmp_path, model="offline").workflow(
        spec, {"events": events, "mode": mode}, llm=ResourceModel(), trace=False,
        cleanup_timeout=2,
    ))
    try:
        await asyncio.wait_for(events["entered"].wait(), 5)
        if mode in {"caller", "repeated"}:
            running.cancel()
        else:
            await asyncio.wait_for(events["returned"].wait(), 5)
        await asyncio.wait_for(events["cleanup_started"].wait(), 5)
        if mode == "repeated":
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()
            await asyncio.sleep(0)
        assert _import_state() != before
        assert not events["closed"].is_set()
        events["release"].set()
        if mode in {"caller", "repeated"}:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
        else:
            result = await asyncio.wait_for(running, 5)
            assert result.status == "completed", result.reason
            assert result.output == "recovered"
        assert events["closed"].is_set()
        assert _import_state() == before
    finally:
        events["release"].set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_cleanup_deadline_keeps_imports_for_lingering_owned_work(tmp_path):
    before = _import_state()
    spec = _loaded_workflow(tmp_path)
    events = _events()
    running = asyncio.create_task(OpenCollab(tmp_path, model="offline").workflow(
        spec, {"events": events, "mode": "recover"}, llm=ResourceModel(), trace=False,
        cleanup_timeout=0.01,
    ))
    try:
        await asyncio.wait_for(events["returned"].wait(), 5)
        with pytest.raises(RunError, match="without finalized execution evidence"):
            await asyncio.wait_for(asyncio.shield(running), 5)
        assert _import_state() != before
        assert not events["closed"].is_set()
        events["release"].set()
        await asyncio.wait_for(events["closed"].wait(), 5)
        await asyncio.wait_for(events["context"].wait_for_pending_cleanup(), 5)
        assert _import_state() == before
    finally:
        events["release"].set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_concurrent_loaded_workflows_retain_each_others_cleanup_imports(tmp_path):
    before = _import_state()
    spec = _loaded_workflow(tmp_path)
    client = OpenCollab(tmp_path, model="offline")
    events = [_events(), _events()]
    calls = [asyncio.create_task(client.workflow(
        spec, {"events": signals, "mode": "recover"}, llm=ResourceModel(), trace=False,
    )) for signals in events]
    try:
        await asyncio.wait_for(asyncio.gather(*(signals["returned"].wait() for signals in events)), 5)
        events[0]["release"].set()
        assert (await asyncio.wait_for(calls[0], 5)).status == "completed"
        assert events[0]["closed"].is_set()
        assert not events[1]["closed"].is_set()
        assert _import_state() != before
        events[1]["release"].set()
        assert (await asyncio.wait_for(calls[1], 5)).status == "completed"
        assert events[1]["closed"].is_set()
        assert _import_state() == before
    finally:
        for signals in events:
            signals["release"].set()
        await asyncio.gather(*calls, return_exceptions=True)


async def test_candidate_child_context_retains_loaded_workflow_imports(tmp_path):
    before = _import_state()
    spec = _loaded_workflow(tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    for arguments in (
        ("init", "-q"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "test: create workspace"),
    ):
        subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True)
    events = _events()

    async def parent(ctx, args):
        return await ctx.candidate_workflow(spec.fn, args, label="resource-worker")

    running = asyncio.create_task(OpenCollab(workspace, model="offline").workflow(
        parent, {"events": events, "mode": "recover"}, llm=ResourceModel(), trace=False,
        cleanup_timeout=5,
    ))
    try:
        await asyncio.wait_for(events["returned"].wait(), 15)
        assert _import_state() != before
        events["release"].set()
        result = await asyncio.wait_for(running, 15)
        assert result.status == "completed", result.reason
        assert result.output.output == "recovered"
        assert events["closed"].is_set()
        assert _import_state() == before
    finally:
        events["release"].set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
