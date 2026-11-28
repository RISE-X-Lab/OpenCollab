from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.async_timeout import await_owned_operation
from tests.interfaces.test_session_isolation import _repo


@pytest.mark.parametrize("stop,borrow_source,entry", [
    ("none", False, "managed"),
    ("stage", False, "managed"),
    ("deadline", False, "managed"),
    ("caller", False, "managed"),
    ("caller", True, "managed"),
    ("stage", False, "plain"),
    ("stage", False, "enforced"),
    ("stage", False, "schema"),
])
async def test_workflow_retains_ownership_while_an_isolated_agent_is_starting(
    tmp_path, monkeypatch, stop, borrow_source, entry,
):
    workspace = tmp_path / "source"
    workspace.mkdir()
    _repo(workspace, dirty=False)
    entered, continue_setup, setup_finished, pool_released, stage_finished, return_stage = (
        asyncio.Event() for _ in range(6)
    )
    setup_state = {}
    requests = []
    stages = []
    cancellations = []

    class InventoryModel:
        def context_window(self):
            return 100_000

        async def complete(self, messages, **kwargs):
            requests.append(messages)
            return LLMResponse(content="Inventory verified.", usage=Usage(3, 2), finish_reason="stop")

    class DelayedWorktreePool(WorktreePool):
        async def acquire(self, role, *, source=None):
            async def configure_checkout():
                env = await super(DelayedWorktreePool, self).acquire(role, source=source)
                setup_state.update(env=env, source=source)
                entered.set()
                await continue_setup.wait()
                try:
                    setup_state["source_text"] = await source.read_file("seed.txt")
                    setup_state["checkout_text"] = await env.read_file("seed.txt")
                finally:
                    setup_finished.set()
                return env

            return await await_owned_operation(configure_checkout(), propagate_cancellation=not borrow_source)

        async def release(self):
            await super().release()
            pool_released.set()

    def provider(**options):
        return DelayedWorktreePool(use_worktrees=True, **options)

    async def flow(ctx, _):
        options = dict(label="verifier", tools=[], isolation="inventory", budget=15_000,
                       timeout=.9 if stop == "stage" else 5)
        if entry == "managed":
            stage = await ctx.agent_run("Check the inventory.", cleanup_timeout=.06, **options)
        else:
            if entry == "enforced":
                options["enforcement_strength"] = "needs-enforcement"
            if entry == "schema":
                options["schema"] = {"type": "object", "properties": {"verified": {"type": "boolean"}}}
            stage = await ctx.agent("Check the inventory.", **options)
        stages.append(stage)
        stage_finished.set()
        await return_stage.wait()
        return stage.output if entry == "managed" else stage

    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    borrowed = LocalEnvironment(str(workspace)) if borrow_source else None
    client = OpenCollab(
        workspace, model="gpt-4o", environment=borrowed, environment_providers={"inventory": provider},
    )
    artifacts = tmp_path / "artifacts"
    if stop == "none":
        continue_setup.set()
        return_stage.set()
    async def run():
        try:
            return await client.workflow(
                flow, llm=InventoryModel(), budget=30_000, limit_mode="explicit", artifacts=artifacts,
                trace=False, timeout=1.6 if stop == "deadline" else None, cleanup_timeout=.08,
            )
        except asyncio.CancelledError as exc:
            cancellations.append(exc.args)
            raise

    operation = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        checkout = Path(setup_state["env"].workspace)
        source = setup_state["source"]
        if stop == "stage":
            await asyncio.wait_for(stage_finished.wait(), 3)
            stage, = stages
            if entry == "managed":
                assert (stage.status, stage.reason, stage.output, stage.tokens) == ("stopped", "timeout", None, 0)
                assert not stage.cleanup_complete and not stage.workspace_ready
            else:
                assert stage is None
        elif stop == "caller":
            operation.cancel("caller stopped worktree preparation")
            done, _ = await asyncio.wait({operation}, timeout=3)
            assert operation in done
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert cancellations == [("caller stopped worktree preparation",)]
        else:
            result = await asyncio.wait_for(asyncio.shield(operation), 3)
            if stop == "deadline":
                assert (result.status, result.reason, result.output, result.tokens) == ("stopped", "timeout", None, 0)
                assert result.metrics["execution_quiesced"] is False
            else:
                assert (result.status, result.output, result.tokens) == ("completed", "Inventory verified.", 5)

        manifest = artifacts / "workflow.json"
        if stop != "none":
            assert not setup_finished.is_set() and not pool_released.is_set()
            assert checkout.is_dir() and not source.revoked
            assert not requests
        if stop in {"deadline", "caller"}:
            snapshot = json.loads(manifest.read_text())
            assert (snapshot["status"], snapshot["reason"]) == ("stopped", "cancelled")
            assert snapshot["sessions"] == snapshot["tokens_spent"] == 0
            assert snapshot["evidence_complete"] is False
        continue_setup.set()
        await asyncio.wait_for(setup_finished.wait(), 3)
        assert setup_state["source_text"] == setup_state["checkout_text"] == "initial"
        if stop == "stage":
            return_stage.set()
            result = await asyncio.wait_for(operation, 3)
            assert (result.status, result.output, result.tokens) == ("completed", None, 0)
        await asyncio.wait_for(pool_released.wait(), 3)
        assert not checkout.exists()
        if borrowed is not None:
            assert source is borrowed and await borrowed.read_file("seed.txt") == "initial"
        if stop in {"none", "stage"}:
            saved = json.loads(manifest.read_text())
            assert saved["evidence_complete"] is True
            assert saved["status"] == "completed"
            assert saved["sessions"] == (1 if stop == "none" else 0)
        assert len(requests) == (1 if stop == "none" else 0)
    finally:
        continue_setup.set()
        return_stage.set()
        await asyncio.gather(operation, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(pool_released.wait(), 5)
        if borrowed is not None:
            await borrowed.cleanup()
