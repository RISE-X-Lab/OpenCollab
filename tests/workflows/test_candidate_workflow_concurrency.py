"""Candidate orchestration shares agent and task capacity across the run."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from opencollab import OpenCollab
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.workflow import WorkflowContext
from tests.support.workflow_context_test_support import (
    CancelCleanupSession,
    FakeFactory,
    FakeSession,
)


def _repository(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    (root / "source.txt").write_text("source\n")
    subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    return root


def _context(repository: Path, sessions: list[FakeSession], concurrency: int) -> WorkflowContext:
    environment = LocalEnvironment(str(repository))
    return WorkflowContext(
        FakeFactory(sessions),
        max_concurrency=concurrency,
        task_concurrency=concurrency,
        candidate_workspace=EnvCandidateWorkspace(environment),
    )


@pytest.mark.asyncio
async def test_parallel_candidate_children_share_parent_agent_session_cap(tmp_path: Path) -> None:
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    candidates_started = 0
    both_candidates_started = asyncio.Event()
    release = asyncio.Event()

    async def entered() -> None:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running >= 2:
            admitted.set()
        try:
            await release.wait()
        finally:
            running -= 1

    sessions = [FakeSession(on_enter=entered) for _ in range(4)]
    parent = _context(_repository(tmp_path / "repo"), sessions, concurrency=2)

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        nonlocal candidates_started
        candidates_started += 1
        if candidates_started == 2:
            both_candidates_started.set()
        await child.parallel([lambda: child.agent("one"), lambda: child.agent("two")])
        return "done"

    async def candidate(label: str) -> Any:
        try:
            return await parent.candidate_workflow(nested, {}, label=label)
        except Exception as exc:
            return exc

    task = asyncio.create_task(parent.parallel([
        lambda: candidate("A"),
        lambda: candidate("B"),
    ]))
    try:
        await asyncio.wait_for(both_candidates_started.wait(), timeout=5)
        await asyncio.wait_for(admitted.wait(), timeout=5)
        for _ in range(20):
            await asyncio.sleep(0)
        assert high_water == 2
    finally:
        release.set()
    results = await asyncio.wait_for(task, timeout=10)

    assert [getattr(result, "output", None) for result in results] == ["done", "done"], results
    assert high_water == 2


@pytest.mark.asyncio
async def test_nested_candidate_pipelines_share_workflow_task_cap(tmp_path: Path) -> None:
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    both_candidates_entered = asyncio.Event()
    candidates_entered = 0
    release = asyncio.Event()
    parent = _context(_repository(tmp_path / "repo"), [], concurrency=2)

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        nonlocal candidates_entered
        candidates_entered += 1
        if candidates_entered == 2:
            both_candidates_entered.set()

        async def gated(value: int, _item: int, _index: int) -> int:
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            if running >= 2:
                admitted.set()
            try:
                await release.wait()
                return value
            finally:
                running -= 1

        await child.pipeline([1, 2], gated)
        return "done"

    task = asyncio.create_task(parent.parallel([
        lambda: parent.candidate_workflow(nested, {}, label="A"),
        lambda: parent.candidate_workflow(nested, {}, label="B"),
    ]))
    try:
        await asyncio.wait_for(both_candidates_entered.wait(), timeout=5)
        await asyncio.wait_for(admitted.wait(), timeout=5)
        for _ in range(20):
            await asyncio.sleep(0)
        assert high_water <= 2
    finally:
        release.set()
    results = await asyncio.wait_for(task, timeout=10)

    assert [result.output for result in results] == ["done", "done"]
    assert high_water == 2


@pytest.mark.asyncio
async def test_nested_candidate_pipeline_borrows_parent_task_slot_at_cap_one(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    parent = _context(_repository(tmp_path / "repo"), [], concurrency=1)

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        async def gated(value: int, _item: int, _index: int) -> int:
            entered.set()
            await release.wait()
            return value

        values = await child.pipeline([1, 2], gated)
        return ",".join(map(str, values))

    task = asyncio.create_task(parent.parallel([
        lambda: parent.candidate_workflow(nested, {}, label="nested"),
    ]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        release.set()
    results = await asyncio.wait_for(task, timeout=10)

    assert results[0].output == "1,2"


@pytest.mark.asyncio
async def test_candidate_child_parallelism_uses_available_cap_without_deadlock(tmp_path: Path) -> None:
    running = 0
    high_water = 0
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def entered() -> None:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running == 2:
            admitted.set()
        try:
            await release.wait()
        finally:
            running -= 1

    parent = _context(
        _repository(tmp_path / "repo"),
        [FakeSession(on_enter=entered) for _ in range(2)],
        concurrency=2,
    )

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        await child.parallel([lambda: child.agent("one"), lambda: child.agent("two")])
        return "done"

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=5)
        assert high_water == 2
    finally:
        release.set()

    result = await asyncio.wait_for(task, timeout=10)
    assert result.output == "done"
    assert high_water == 2


@pytest.mark.asyncio
async def test_candidate_child_finishes_with_single_session_slot(tmp_path: Path) -> None:
    parent = _context(
        _repository(tmp_path / "repo"),
        [FakeSession(), FakeSession()],
        concurrency=1,
    )

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        results = await child.parallel([lambda: child.agent("one"), lambda: child.agent("two")])
        return ",".join(str(result) for result in results)

    result = await asyncio.wait_for(
        parent.candidate_workflow(nested, {}, label="candidate"),
        timeout=5,
    )

    assert result.output == "done,done"


@pytest.mark.asyncio
async def test_parent_agent_overlaps_candidate_child_within_shared_cap(tmp_path: Path) -> None:
    running = 0
    high_water = 0
    both_entered = asyncio.Event()
    parent_entered = asyncio.Event()
    release = asyncio.Event()

    async def entered() -> None:
        nonlocal running, high_water
        running += 1
        high_water = max(high_water, running)
        if running == 2:
            both_entered.set()
        try:
            await release.wait()
        finally:
            running -= 1

    async def parent_session_entered() -> None:
        parent_entered.set()
        await entered()

    parent = _context(
        _repository(tmp_path / "repo"),
        [FakeSession(on_enter=parent_session_entered), FakeSession(on_enter=entered)],
        concurrency=2,
    )

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        return str(await child.agent("candidate child"))

    parent_task = asyncio.create_task(parent.agent("parent agent"))
    candidate_task = None
    try:
        await asyncio.wait_for(parent_entered.wait(), timeout=5)
        candidate_task = asyncio.create_task(
            parent.candidate_workflow(nested, {}, label="candidate")
        )
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        for _ in range(10):
            await asyncio.sleep(0)
        assert high_water == 2
    finally:
        release.set()
        pending = [task for task in (parent_task, candidate_task) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)

    assert await parent_task == "done"
    assert candidate_task is not None
    assert (await candidate_task).output == "done"
    assert high_water == 2


@pytest.mark.asyncio
async def test_timed_out_candidate_child_keeps_single_slot_until_cleanup_finishes(
    tmp_path: Path,
) -> None:
    child_session = CancelCleanupSession()
    parent_started = asyncio.Event()

    async def parent_entered() -> None:
        parent_started.set()

    parent_session = FakeSession(on_enter=parent_entered)
    factory = FakeFactory([child_session, parent_session])
    repository = _repository(tmp_path / "repo")
    environment = LocalEnvironment(str(repository))
    parent = WorkflowContext(
        factory,
        max_concurrency=1,
        task_concurrency=1,
        candidate_workspace=EnvCandidateWorkspace(environment),
    )
    candidate_returned_from_agent = asyncio.Event()

    async def nested(child: Any, _args: dict[str, Any]) -> str:
        await child.agent("slow child", timeout=0.01)
        candidate_returned_from_agent.set()
        return "done"

    candidate_task = asyncio.create_task(
        parent.candidate_workflow(nested, {}, label="candidate")
    )
    parent_task = None
    try:
        await asyncio.wait_for(candidate_returned_from_agent.wait(), timeout=5)
        await asyncio.wait_for(child_session.cancel_seen.wait(), timeout=5)
        parent_task = asyncio.create_task(parent.agent("parent waits for child cleanup"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(factory.builds) == 1
        assert not parent_started.is_set()
    finally:
        child_session.release_cancel.set()
        pending = [task for task in (candidate_task, parent_task) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        await parent.wait_for_pending_cleanup()

    assert candidate_task.result().output == "done"
    assert parent_task is not None
    assert await parent_task == "done"
    assert parent_started.is_set()
    assert len(factory.builds) == 2


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    (tmp_path / "source.txt").write_text("initial\n")
    for args in (
        ("init", "-q"),
        ("add", "source.txt"),
        ("-c", "user.name=Test User", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "\u521d\u59cb\u5316\u6d4b\u8bd5\u4ed3\u5e93"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("depth", [0, 2])
async def test_public_candidate_tree_respects_provider_capacity(repository, monkeypatch, concurrency, depth):
    active = peak = calls = rejected = 0
    original_client = openai.AsyncOpenAI

    async def respond(request):
        nonlocal active, peak, calls, rejected
        active += 1
        peak = max(peak, active)
        calls += 1
        try:
            if active > concurrency:
                rejected += 1
                return httpx.Response(429, json={
                    "error": {"message": "Concurrent request limit", "type": "rate_limit"},
                })
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={
                "id": "test-response", "object": "chat.completion", "created": 0, "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        finally:
            active -= 1

    def build_client(**kwargs):
        return original_client(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(openai, "AsyncOpenAI", build_client)
    entered = 0
    both_candidates = asyncio.Event()

    async def recursive_roles(ctx, levels):
        if levels == 0:
            return await ctx.agent("leaf agent")
        return await ctx.parallel([lambda: recursive_roles(ctx, levels - 1) for _ in range(2)])

    async def candidate_roles(child, _args):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_candidates.set()
        await both_candidates.wait()
        return await child.parallel([lambda: recursive_roles(child, depth) for _ in range(2)])

    async def workflow(ctx, _args):
        candidates = await ctx.parallel([
            lambda: ctx.candidate_workflow(candidate_roles, {}, label="A"),
            lambda: ctx.candidate_workflow(candidate_roles, {}, label="B"),
        ])
        return [candidate.output for candidate in candidates]

    client = OpenCollab(
        repository, model="test-model", provider="openai",
        api_key="test-key",  # pragma: allowlist secret -- in-memory transport
        base_url="https://provider.example.invalid/v1",
        config={"llm_max_retries": 0},
    )
    result = await asyncio.wait_for(client.workflow(
        workflow, budget=1_000_000, concurrency=concurrency, task_concurrency=2,
        system_prompt="Concurrency test", timeout=None, trace=False,
    ), timeout=10)

    def leaves(value):
        if isinstance(value, list):
            return [leaf for item in value for leaf in leaves(item)]
        return [value]

    expected_calls = 4 * 2 ** depth
    assert result.ok
    assert leaves(result.output) == ["answer"] * expected_calls
    assert calls == expected_calls
    assert rejected == active == 0
    assert peak == concurrency


async def test_candidate_task_capacity_is_independent_of_agent_capacity(repository):
    started = asyncio.Event()
    release = asyncio.Event()
    async def enter_agent():
        started.set()

    session = FakeSession(on_enter=enter_agent, gate=release)
    parent = WorkflowContext(
        FakeFactory([session]), max_concurrency=1, task_concurrency=2,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )
    active = peak = 0
    both_tasks = asyncio.Event()

    async def ordinary_task():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_tasks.set()
        try:
            await release.wait()
            return "ordinary task"
        finally:
            active -= 1

    async def nested(child, _args):
        agent = asyncio.create_task(child.agent("agent"))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            return await child.parallel([ordinary_task, ordinary_task])
        finally:
            await agent

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(both_tasks.wait(), timeout=5)
        assert peak == 2
    finally:
        release.set()
        candidate = await asyncio.wait_for(task, timeout=5)
    assert candidate.output == ["ordinary task", "ordinary task"]
    assert parent.agent_failures == ()


@pytest.mark.parametrize("ending", ["error", "cancel"])
async def test_candidate_exit_returns_shared_agent_slot(repository, ending):
    started = asyncio.Event()
    release = asyncio.Event()

    async def entered():
        started.set()

    first = FakeSession(on_enter=entered, gate=release, boom=ending == "error")
    later = FakeSession(reply="later")
    parent = WorkflowContext(
        FakeFactory([first, later]), max_concurrency=1,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )

    async def nested(child, _args):
        value = await child.agent("candidate agent")
        if ending == "error":
            raise RuntimeError("candidate workflow failed")
        return value

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        else:
            release.set()
            assert (await asyncio.wait_for(task, timeout=5)).output is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert await asyncio.wait_for(parent.agent("later agent"), timeout=5) == "later"
    assert parent.pending_cleanup_tasks == ()


async def test_candidate_and_nested_collection_complete_with_one_slot(repository):
    parent = WorkflowContext(
        FakeFactory([FakeSession(), FakeSession()]), max_concurrency=1,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )

    async def nested(child, _args):
        return await child.parallel([lambda: child.agent("one"), lambda: child.agent("two")])

    candidates = await asyncio.wait_for(parent.parallel([
        lambda: parent.candidate_workflow(nested, {}, label="candidate"),
    ]), timeout=5)

    assert candidates[0].output == ["done", "done"]
    assert parent.agent_failures == ()


@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("outer_collection", [False, True])
async def test_parallel_candidate_collections_share_task_capacity(repository, capacity, outer_collection):
    parent = WorkflowContext(
        FakeFactory([]), max_concurrency=1, task_concurrency=capacity,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )
    active = peak = 0

    async def service():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if active > capacity:
                raise RuntimeError("service capacity exceeded")
            await asyncio.sleep(0.02)
            return "completed"
        finally:
            active -= 1

    async def nested(child, _args):
        return await child.parallel([service, service])

    thunks = [
        lambda: parent.candidate_workflow(nested, {}, label="A"),
        lambda: parent.candidate_workflow(nested, {}, label="B"),
    ]
    pending = parent.parallel(thunks) if outer_collection else asyncio.gather(*(thunk() for thunk in thunks))
    candidates = await asyncio.wait_for(pending, timeout=10)

    assert peak == capacity
    assert [candidate.output for candidate in candidates] == [["completed", "completed"]] * 2
    assert parent.agent_failures == ()


async def test_timed_out_child_holds_shared_slot_until_cleanup_finishes(repository):
    slow = CancelCleanupSession()
    later_entered = asyncio.Event()

    async def enter_later():
        later_entered.set()

    later = FakeSession(reply="later", on_enter=enter_later)
    parent = WorkflowContext(
        FakeFactory([slow, later]), max_concurrency=1,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )

    async def nested(child, _args):
        return await child.agent("slow", timeout=0.5)

    candidate_task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    later_task = None
    try:
        await asyncio.wait_for(slow.started.wait(), timeout=5)
        await asyncio.wait_for(slow.cancel_seen.wait(), timeout=5)
        later_task = asyncio.create_task(parent.agent("later"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not later_entered.is_set()
        assert not candidate_task.done()
    finally:
        slow.release_cancel.set()
        candidate = await asyncio.wait_for(candidate_task, timeout=5)
        if later_task is not None:
            assert await asyncio.wait_for(later_task, timeout=5) == "later"

    assert candidate.output is None
    assert parent.pending_cleanup_tasks == ()
