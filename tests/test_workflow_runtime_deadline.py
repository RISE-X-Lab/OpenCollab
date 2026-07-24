"""Workflow runtime bootstrap deadline tests."""

from __future__ import annotations

import asyncio

import pytest
from workflow_runtime_test_support import (
    _cfg,
    _InjectedEnvironment,
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
