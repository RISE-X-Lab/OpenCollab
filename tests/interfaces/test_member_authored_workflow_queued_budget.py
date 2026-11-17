from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import client as transport
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap.authored_workflow import _Child
from opencollab.bootstrap.session_factory import _team_budget_guard
from opencollab.domain.scheduler import SessionControlBlock
from tests.interfaces.test_member_authored_workflow import _manager, _serve_model
from tests.support.paths import REPO_ROOT


@pytest.mark.parametrize("first_tokens, parent_stop, child_escape", [
    (5_000, None, False), (20_210, None, False), (5_000, "team", False), (5_000, "cancel", False),
    (20_210, None, True),
])
async def test_authored_child_stops_a_queued_request_after_shared_budget_exhaustion(
    tmp_path, monkeypatch, first_tokens, parent_stop, child_escape,
):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "authored.py").write_text('''import asyncio
from pathlib import Path

async def main(ctx, args):
    first = asyncio.create_task(ctx.agent("first", budget=18000, tools=[]))
    if args.get("escape"):
        await first
    while not Path("first-network").exists():
        await asyncio.sleep(0.01)
    second = asyncio.create_task(ctx.agent("second", budget=2000, tools=[], over_budget_ok=args.get("escape", False)))
    answers = await asyncio.gather(first, second)
    return {"answers": answers, "reason": ctx.sessions[1].state.terminal_reason}
''', encoding="utf-8")
    pool = tmp_path / "pool"
    pool.mkdir()
    release, first_entered, queued = threading.Event(), threading.Event(), asyncio.Event()
    native_slot = transport.request_slot
    entries = 0

    @asynccontextmanager
    async def observed_slot(*args):
        nonlocal entries
        entries += 1
        if entries == 2:
            queued.set()
        async with native_slot(*args):
            yield

    monkeypatch.setattr(transport, "request_slot", observed_slot)

    def reply(body):
        first = body["messages"][-1]["content"].startswith("first")
        if first:
            (tmp_path / "first-network").write_text("ready", encoding="utf-8")
            first_entered.set()
            assert release.wait(15)
        total = first_tokens if first else 4
        return ({"role": "assistant", "content": "first answer" if first else "second answer"},
                {"prompt_tokens": total - 2, "completion_tokens": 2, "total_tokens": total})

    environment = LocalEnvironment(str(tmp_path))
    with _serve_model(reply) as (url, requests):
        parent, tool, runtime = _manager(environment, budget=20_000)
        aggregate_exhausted, cancel = asyncio.Event(), asyncio.Event()
        parent.runner._team_budget_exhausted = aggregate_exhausted.is_set
        parent.runner.default_cancel_event = cancel
        tool._config.base_url = parent.agent.base_url = url
        tool._config.api_key = parent.agent.api_key = "controlled-test"  # pragma: allowlist secret
        tool._config.request_pool = parent.agent.request_pool = str(pool)
        tool._config.request_capacity = parent.agent.request_capacity = 1
        parent.agent.max_tokens_per_step = 128
        try:
            launched = json.loads(await tool.execute_with_runtime(
                {"action": "launch", "source": "authored.py", "python": sys.executable,
                 "budget": 20_000, "concurrency": 2, "args": {"escape": child_escape}}, runtime,
            ))
            child = tool._children[launched["id"]]
            if child_escape:
                assert await asyncio.to_thread(first_entered.wait, 15)
            else:
                await asyncio.wait_for(queued.wait(), 15)
            assert len(requests) == 1
            if parent_stop == "team":
                aggregate_exhausted.set()
            elif parent_stop == "cancel":
                cancel.set()
            release.set()
            await asyncio.wait_for(child.task, 15)
            cancelled = parent_stop == "cancel" and child.result["status"] == "stopped"
            if cancelled:
                assert child.result["reason"] == "cancelled"
                assert child.result["output"] is None
                assert len(requests) == 1
            else:
                assert child.result["status"] == "completed", child.result
                assert child.result["agent_failures"] == []
                if first_tokens >= 20_000 or parent_stop is not None:
                    assert len(requests) == 1
                    assert child.result["output"]["answers"] == ["first answer", ""]
                    reason = {"team": "team budget exceeded", "cancel": "interrupted by user"}.get(
                        parent_stop, "budget exhausted before model call",
                    )
                    assert reason in child.result["output"]["reason"]
                else:
                    assert len(requests) == 2
                    assert child.result["output"]["answers"] == ["first answer", "second answer"]
            assert child.bridge.tokens == child.tokens == parent.used_tokens == first_tokens + 4 * (len(requests) - 1)
        finally:
            release.set()
            await parent.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("first_tokens, parent_budget", [(5_000, 20_000), (20_210, 20_000), (20_210, None)])
@pytest.mark.parametrize("separate_parents", [False, True], ids=["one-role", "two-team-roles"])
async def test_parallel_authored_children_share_fresh_parent_budget(
    tmp_path, monkeypatch, first_tokens, parent_budget, separate_parents,
):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    (tmp_path / "authored.py").write_text(
        'async def main(ctx, args):\n    return await ctx.agent(args["prompt"], tools=[], budget=10000)\n',
        encoding="utf-8",
    )
    pool = tmp_path / "pool"
    pool.mkdir()
    first_entered, release = threading.Event(), threading.Event()
    queued, released_slot = asyncio.Event(), asyncio.Event()
    native_slot = transport.request_slot
    entries = 0

    @asynccontextmanager
    async def ordered_slot(*args):
        nonlocal entries
        entries += 1
        index = entries
        if index > 1:
            queued.set()
            await released_slot.wait()
        async with native_slot(*args):
            yield
        if index == 1:
            released_slot.set()

    monkeypatch.setattr(transport, "request_slot", ordered_slot)

    observed_spend = []

    def reply(body):
        first = body["messages"][-1]["content"].startswith("first")
        observed_spend.append(sum(parent.used_tokens for parent, _tool, _runtime in managers))
        if first:
            first_entered.set()
            assert release.wait(15)
        total = first_tokens if first else 4
        return ({"role": "assistant", "content": "answer"},
                {"prompt_tokens": total - 2, "completion_tokens": 2, "total_tokens": total})

    environment = LocalEnvironment(str(tmp_path))
    managers = []
    scheduler = Scheduler(session_factory=None, worktree_pool=None, event_sink=None, max_budget_tokens=20_000)
    with _serve_model(reply) as (url, requests):
        allowance = 10_000 if separate_parents and parent_budget is not None else parent_budget
        for _ in range(2 if separate_parents else 1):
            parent, tool, runtime = _manager(environment, budget=allowance)
            if separate_parents:
                parent.runner._team_budget_exhausted = _team_budget_guard(scheduler)
                aid = scheduler.table.allocate_aid()
                scheduler.table.add(SessionControlBlock(
                    aid=aid, parent_aid=None, agent=parent.agent, state=parent.state,
                ))
                scheduler._sessions[aid] = parent
            tool._config.base_url = parent.agent.base_url = url
            tool._config.api_key = parent.agent.api_key = "controlled-test"  # pragma: allowlist secret
            tool._config.request_pool = parent.agent.request_pool = str(pool)
            tool._config.request_capacity = parent.agent.request_capacity = 1
            parent.agent.max_tokens_per_step = 128
            managers.append((parent, tool, runtime))
        children = []
        try:
            for index, prompt in enumerate(("first", "second")):
                parent, tool, runtime = managers[index if separate_parents else 0]
                launched = json.loads(await tool.execute_with_runtime(
                    {"action": "launch", "source": "authored.py", "python": sys.executable,
                     "budget": 10_000, "args": {"prompt": prompt}}, runtime,
                ))
                children.append(tool._children[launched["id"]])
                if prompt == "first":
                    assert await asyncio.to_thread(first_entered.wait, 15)
            await asyncio.wait_for(queued.wait(), 15)
            assert len(requests) == 1
            release.set()
            await asyncio.wait_for(asyncio.gather(*(child.task for child in children)), 20)
            admitted = (parent_budget is None and not separate_parents) or first_tokens < 20_000
            assert len(requests) == (2 if admitted else 1)
            assert all(child.result["status"] == "completed" for child in children)
            assert children[1].result["output"] == ("answer" if admitted else "")
            assert children[0].tokens == children[0].bridge.tokens == first_tokens
            assert children[1].tokens == children[1].bridge.tokens == (4 if admitted else 0)
            total = first_tokens + (4 if admitted else 0)
            assert sum(parent.used_tokens for parent, _tool, _runtime in managers) == total
            if admitted:
                assert observed_spend[1] == first_tokens
            if separate_parents:
                assert scheduler.used_tokens == first_tokens + (4 if admitted else 0)
        finally:
            release.set()
            for parent, _tool, _runtime in managers:
                await parent.aclose()
            await environment.cleanup()


async def test_host_usage_and_stale_worker_reports_are_deduplicated(tmp_path):
    environment = LocalEnvironment(str(tmp_path))
    artifacts = tmp_path / "child"
    artifacts.mkdir()
    parent, tool, _runtime = _manager(environment, budget=1_000)
    child = _Child("child", environment, str(artifacts), 400)
    try:
        tool._account_usage(child, 90, reported=False)
        assert parent.used_tokens == child.tokens == 90
        assert child.reported_tokens == 0
        for reported in (0, 30, 30, 60, 90):
            (artifacts / "usage.json").write_text(json.dumps({"tokens": reported}), encoding="utf-8")
            await tool._refresh_usage(child)
            assert parent.used_tokens == child.tokens == 90
            assert child.reported_tokens == reported
        tool._account_usage(child, 100, reported=False)
        tool._account_usage(child, 90)
        assert child.reported_tokens == 90
        tool._account_usage(child, 100)
        assert parent.used_tokens == child.tokens == child.reported_tokens == 100
        for invalid in (99, -1, True, 100.0, "100"):
            with pytest.raises(ValueError, match="invalid child usage total"):
                tool._account_usage(child, invalid)
            assert parent.used_tokens == child.tokens == child.reported_tokens == 100
    finally:
        await parent.aclose()
        await environment.cleanup()
