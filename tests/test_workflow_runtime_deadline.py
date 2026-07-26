"""Workflow runtime bootstrap deadline tests."""

from __future__ import annotations

import asyncio

import pytest
from workflow_runtime_test_support import (
    _cfg,
    _FakeSession,
    _InjectedEnvironment,
)

from opencollab.bootstrap import (
    _workflow_runtime_session as workflow_session,
)
from opencollab.bootstrap import (
    workflow_runtime,
)


@pytest.mark.asyncio
async def test_bootstrap_deadline_cancels_owner_and_aborts_injected_environment(monkeypatch):
    cancelled = asyncio.Event()
    environment = _InjectedEnvironment()

    async def blocked(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(workflow_runtime, "_run_workflow_with_integrity", blocked)
    with pytest.raises(workflow_runtime.WorkflowDeadlineExceeded):
        await workflow_runtime.run_workflow(
            object(),
            {},
            cfg=_cfg(),
            env=environment,
            deadline_monotonic=asyncio.get_running_loop().time() + 0.01,
            cleanup_timeout=0.1,
        )
    assert cancelled.is_set()
    assert environment.revoked
    assert environment.abort_calls == 1

@pytest.mark.asyncio
async def test_bootstrap_deadline_reports_lifecycle_failure_when_abort_fails(monkeypatch):
    environment = _InjectedEnvironment(abort_fails=True)

    async def blocked(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow_runtime, "_run_workflow_with_integrity", blocked)
    with pytest.raises(workflow_runtime.WorkflowLifecycleError, match="quiescent"):
        await workflow_runtime.run_workflow(
            object(),
            {},
            cfg=_cfg(),
            env=environment,
            deadline_monotonic=asyncio.get_running_loop().time() + 0.01,
            cleanup_timeout=0.1,
        )
    assert environment.revoked
    assert environment.abort_calls == 1

@pytest.mark.asyncio
async def test_bootstrap_deadline_preserves_inner_cleanup_failure(monkeypatch):
    environment = _InjectedEnvironment()

    async def failed_cleanup(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            environment.revoke()
            raise RuntimeError("inner cleanup failed")

    monkeypatch.setattr(
        workflow_runtime,
        "_run_workflow_with_integrity",
        failed_cleanup,
    )
    with pytest.raises(
        workflow_runtime.WorkflowLifecycleError,
        match="terminal state",
    ) as caught:
        await workflow_runtime.run_workflow(
            object(),
            {},
            cfg=_cfg(),
            env=environment,
            deadline_monotonic=asyncio.get_running_loop().time() + 0.01,
            cleanup_timeout=0.1,
        )
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "inner cleanup failed"
    assert environment.abort_calls == 0

@pytest.mark.asyncio
async def test_bootstrap_preserves_inner_timeout(monkeypatch):
    inner = asyncio.TimeoutError("provider timeout")

    async def fail(*_args, **_kwargs):
        raise inner

    monkeypatch.setattr(workflow_runtime, "_run_workflow_with_integrity", fail)
    with pytest.raises(asyncio.TimeoutError) as captured:
        await workflow_runtime.run_workflow(
            object(),
            {},
            cfg=_cfg(),
            deadline_monotonic=asyncio.get_running_loop().time() + 1,
        )
    assert captured.value is inner


@pytest.mark.asyncio
async def test_bootstrap_deadline_preserves_finalized_session_metrics(
    monkeypatch,
    tmp_path,
):
    session_finished = asyncio.Event()

    async def wait_after_session(owner, *, timeout):
        await asyncio.wait_for(session_finished.wait(), timeout=1)
        return set(), {owner}

    monkeypatch.setattr(workflow_runtime, "_wait_for_owner", wait_after_session)

    def fake_build_session(*, agent, **_kwargs):
        session = _FakeSession(agent, agent.tools)
        session.used_tokens = 8
        session.step_count = 4
        session.markup_recovered = 2
        return session

    monkeypatch.setattr(workflow_session, "build_session", fake_build_session)

    async def one_then_block(ctx, _args):
        await ctx.agent("one")
        session_finished.set()
        await asyncio.Event().wait()

    with pytest.raises(workflow_runtime.WorkflowDeadlineExceeded) as caught:
        await workflow_runtime.run_workflow(
            one_then_block,
            {},
            cfg=_cfg(),
            workspace=str(tmp_path),
            deadline_monotonic=asyncio.get_running_loop().time() + 10,
            cleanup_timeout=0.2,
            return_details=True,
        )

    details = caught.value.result
    assert details is not None
    assert details.tokens == 8
    assert details.sessions == 1
    assert details.steps == 4
    assert details.markup_recovered == 2
