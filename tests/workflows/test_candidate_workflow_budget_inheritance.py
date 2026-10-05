"""Candidate workflows consume their allocated share of the parent pool."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from opencollab import OpenCollab
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.bootstrap._workflow_runtime_session import WorkflowSessionFactory
from tests.support.workflow_context_test_support import FakeFactory, FakeSession


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.delenv("OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT", raising=False)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    (tmp_path / "source.txt").write_text("initial\n")
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "source.txt"),
        ("-c", "commit.gpgsign=false", "commit", "-qm", "\u521d\u59cb\u5316\u6d4b\u8bd5\u4ed3\u5e93"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _context(repository, factory, total):
    return WorkflowContext(
        factory,
        budget_total=total,
        max_concurrency=2,
        candidate_workspace=EnvCandidateWorkspace(LocalEnvironment(str(repository))),
    )


@pytest.mark.parametrize(
    "total,spent,requested,approved",
    [
        (2000, 0, None, 2000),
        (2000, 1300, None, 700),
        (2000, 1300, 1800, 700),
        (2000, 1300, 500, 500),
        (None, 0, None, None),
        (None, 0, 500, 500),
    ],
)
async def test_child_inherits_approved_budget(repository, total, spent, requested, approved):
    factory = FakeFactory([FakeSession(tokens=spent), FakeSession(tokens=7)])
    parent = _context(repository, factory, total)
    await parent.agent("earlier work")

    async def nested(child, _args):
        await child.agent("candidate work")
        return child.budget.total

    candidate = await parent.candidate_workflow(nested, {}, label="candidate", budget=requested)

    assert candidate.output == approved
    assert parent.agent_failures == ()
    assert factory.builds[-1]["budget"] == approved
    assert parent.tokens_spent() == spent + 7
    assert len(parent.sessions) == 2
    assert parent.tokens_remaining() == (float("inf") if total is None else total - spent - 7)


@pytest.mark.parametrize("requested", [None, 60])
async def test_parallel_candidates_retain_their_approved_shares(repository, requested):
    factory = FakeFactory([FakeSession(tokens=30), FakeSession(tokens=30)])
    parent = _context(repository, factory, 100)
    children = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def nested(child, _args):
        await child.agent("consume part of the share")
        children.append(child)
        if len(children) == 2:
            both_started.set()
        await release.wait()
        return "done"

    task = asyncio.create_task(parent.parallel([
        lambda: parent.candidate_workflow(nested, {}, label="A", budget=requested),
        lambda: parent.candidate_workflow(nested, {}, label="B", budget=requested),
    ]))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=10)
        approved = sorted(child.budget.total for child in children)
        assert approved == ([50, 50] if requested is None else [40, 60])
        assert sorted(build["budget"] for build in factory.builds) == approved
        # Consumption in a still-running child must not reissue its reservation.
        assert parent.tokens_remaining() == 0
        with pytest.raises(WorkflowBudgetExceeded):
            await parent._acquire_budget_lease(None, over_budget_ok=False)
    finally:
        release.set()
        results = await asyncio.wait_for(task, timeout=10)

    assert all(result.output == "done" for result in results)
    assert parent.tokens_spent() == 60
    assert parent.tokens_remaining() == 40


async def test_over_budget_escape_is_shared_by_sibling_candidate_workflows(repository):
    factory = FakeFactory([
        FakeSession(tokens=10),
        FakeSession(tokens=10),
        FakeSession(tokens=7),
        FakeSession(tokens=7),
        FakeSession(tokens=7),
    ])
    parent = _context(repository, factory, 20)
    children = []
    ready = asyncio.Event()
    release = asyncio.Event()

    async def nested(child, _args):
        children.append(child)
        await child.agent("consume candidate share")
        if len(children) == 2:
            ready.set()
        await release.wait()
        try:
            return await child.agent("forced final write", over_budget_ok=True)
        except WorkflowBudgetExceeded:
            return "refused"

    task = asyncio.create_task(parent.parallel([
        lambda: parent.candidate_workflow(nested, {}, label="A"),
        lambda: parent.candidate_workflow(nested, {}, label="B"),
    ]))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
    finally:
        release.set()
    results = await asyncio.wait_for(task, timeout=10)

    assert sorted(result.output for result in results) == ["done", "refused"]
    assert len(factory.builds) == 3
    assert parent.tokens_spent() == 27
    assert parent.tokens_remaining() == -7
    with pytest.raises(WorkflowBudgetExceeded, match="escape has already been used"):
        await parent.agent("another forced write", over_budget_ok=True)
    assert len(factory.builds) == 3


@pytest.mark.parametrize("parallel", [False, True])
async def test_nested_roles_share_child_pool(repository, parallel):
    factory = FakeFactory([FakeSession(tokens=30), FakeSession(tokens=30)])
    parent = _context(repository, factory, 100)

    async def nested(child, _args):
        if parallel:
            await child.parallel([lambda: child.agent("first"), lambda: child.agent("second")])
        else:
            await child.agent("first")
            await child.agent("second")
        return child.tokens_remaining()

    candidate = await parent.candidate_workflow(nested, {}, label="candidate")

    assert candidate.output == 40
    assert [build["budget"] for build in factory.builds] == ([50, 50] if parallel else [100, 70])
    assert parent.tokens_spent() == 60
    assert parent.tokens_remaining() == 40


@pytest.mark.parametrize("unbounded", [False, True])
@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "2"])
async def test_invalid_candidate_budget_is_rejected(repository, monkeypatch, unbounded, invalid):
    if unbounded:
        monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    factory = FakeFactory([])
    parent = _context(repository, factory, 100)
    entered = []

    async def nested(_child, _args):
        entered.append(True)

    with pytest.raises(ValueError, match="budget must be a positive integer"):
        await parent.candidate_workflow(nested, {}, label="candidate", budget=invalid)

    assert entered == []
    assert factory.builds == []
    assert parent.tokens_remaining() == 100


async def test_exhausted_parent_does_not_start_candidate(repository):
    factory = FakeFactory([FakeSession(tokens=100)])
    parent = _context(repository, factory, 100)
    await parent.agent("use the pool")
    entered = []

    async def nested(_child, _args):
        entered.append(True)

    with pytest.raises(WorkflowBudgetExceeded):
        await parent.candidate_workflow(nested, {}, label="candidate")

    assert entered == []
    assert len(factory.builds) == 1
    assert parent.tokens_remaining() == 0


@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_candidate_releases_reservation_after_exit(repository, ending):
    factory = FakeFactory([FakeSession(tokens=7), FakeSession()])
    parent = _context(repository, factory, 100)
    started = asyncio.Event()
    release = asyncio.Event()

    async def nested(child, _args):
        started.set()
        await release.wait()
        await child.agent("candidate work")
        if ending == "error":
            raise RuntimeError("candidate failed")
        return "done"

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert parent.tokens_remaining() == 0
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        else:
            release.set()
            candidate = await asyncio.wait_for(task, timeout=10)
            assert candidate.output == ("done" if ending == "success" else None)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    expected_spent = 0 if ending == "cancel" else 7
    assert parent.tokens_spent() == expected_spent
    assert parent.tokens_remaining() == 100 - expected_spent
    await parent.agent("later work")
    assert factory.builds[-1]["budget"] == 100 - expected_spent


@pytest.mark.parametrize("fail", [False, True])
async def test_parent_reservation_waits_for_child_cleanup(repository, fail):
    release_cleanup = asyncio.Event()
    cleanup = asyncio.create_task(release_cleanup.wait())
    finished_role = asyncio.Event()
    session = FakeSession(tokens=7)
    session.pending_cleanup_tasks = (cleanup,)
    parent = _context(repository, FakeFactory([session]), 100)

    async def nested(child, _args):
        await child.agent("candidate work")
        finished_role.set()
        if fail:
            raise RuntimeError("workflow failed after role completion")
        return "done"

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    try:
        await asyncio.wait_for(finished_role.wait(), timeout=10)
        assert not task.done()
        assert parent.tokens_remaining() == 0
    finally:
        release_cleanup.set()
        candidate = await asyncio.wait_for(task, timeout=10)
        await cleanup

    assert candidate.output == (None if fail else "done")
    assert parent.tokens_spent() == 7
    assert parent.tokens_remaining() == 93
    assert len(parent.sessions) == 1
    assert parent.pending_cleanup_tasks == ()


@pytest.mark.parametrize("unbounded", [False, True])
@pytest.mark.parametrize("mode", ["ordinary", "candidate-agent", "candidate-workflow", "candidate-cap"])
async def test_public_sdk_enforces_finite_and_preserves_unbounded_sessions(
    repository, monkeypatch, unbounded, mode,
):
    if unbounded:
        monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    calls = []
    sessions = []
    totals = []
    outputs = []
    original_build = WorkflowSessionFactory.build_workflow_session

    async def complete(llm, messages, **kwargs):
        cap = kwargs["max_output_tokens"]
        calls.append((cap, llm.context_window()))
        output = min(1000, cap)
        return LLMResponse(
            content="x" * (output * 4),
            usage=Usage(input_tokens=1, output_tokens=output),
            finish_reason="stop",
        )

    def build(factory, **kwargs):
        session = original_build(factory, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(LLMClient, "complete", complete)
    monkeypatch.setattr(WorkflowSessionFactory, "build_workflow_session", build)

    async def roles(ctx, _args):
        totals.append(ctx.budget.total)
        for index in range(3):
            if mode == "candidate-agent":
                value = await ctx.candidate_agent("work", label=f"role-{index}")
                outputs.append(value.output)
            else:
                outputs.append(await ctx.agent("work", label=f"role-{index}"))
        return "finished"

    async def outer(ctx, args):
        totals.append(ctx.budget.total)
        if mode in {"candidate-workflow", "candidate-cap"}:
            candidate = await ctx.candidate_workflow(
                roles, args, label="candidate", budget=1500 if mode == "candidate-cap" else None,
            )
            return candidate.output
        return await roles(ctx, args)

    client = OpenCollab(
        workspace=repository,
        model="test-model",
        provider="openai",
        api_key="test-key",  # pragma: allowlist secret -- local model stub
        environment=LocalEnvironment(str(repository)),
        config={"max_output_tokens": 65536, "context_window": 1048576},
    )
    result = await client.workflow(
        outer, {}, budget=2000, max_steps=1, timeout=None, concurrency=1,
        system_prompt="budget regression test", trace=False,
    )

    if unbounded:
        assert result.status == "completed"
        assert result.output == "finished"
        assert totals == [None, None]
        assert len(sessions) == 3
        assert all(session.max_budget_tokens is None and session.max_steps is None for session in sessions)
        assert calls == [(65536, 1048576)] * 3
        assert result.tokens == 3003
        assert len(outputs) == 3 and all(outputs)
    else:
        approved = 1500 if mode == "candidate-cap" else 2000
        assert totals == [2000, approved]
        assert 0 < result.tokens <= approved
        assert len(calls) == 2
        assert all(
            session.max_budget_tokens is not None and session.max_budget_tokens <= approved
            for session in sessions
        )
        assert all(session.max_steps == 1 for session in sessions)
        assert all(context == 1048576 for _, context in calls)
        assert len(outputs) == 3 and not outputs[-1]


async def test_cancelled_candidate_keeps_completed_sessions_and_spend(repository):
    factory = FakeFactory([FakeSession(tokens=20), FakeSession(tokens=30), FakeSession()])
    parent = _context(repository, factory, 100)
    await parent.agent("earlier work")
    completed = asyncio.Event()
    release = asyncio.Event()

    async def nested(child, _args):
        await child.agent("finished candidate role")
        completed.set()
        await release.wait()

    task = asyncio.create_task(parent.candidate_workflow(nested, {}, label="candidate"))
    await asyncio.wait_for(completed.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert parent.tokens_spent() == 50
    assert len(parent.sessions) == 2
    assert parent.pending_cleanup_tasks == ()
    await parent.agent("later work")
    assert factory.builds[-1]["budget"] == 50
