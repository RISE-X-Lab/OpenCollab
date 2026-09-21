"""Prebuilt roster creation obeys the scheduler's shutdown boundary."""

from __future__ import annotations

import asyncio

import pytest
from test_prebuilt_team_topology import _roles, _scheduler


@pytest.mark.asyncio
async def test_prebuilding_after_cleanup_does_not_create_new_roles(tmp_path):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    try:
        await scheduler.cleanup()
        with pytest.raises(RuntimeError, match="shutting down"):
            await scheduler.ensure_team_prebuilt()
        assert _roles(scheduler) == {0: "analyst"}
    finally:
        scheduler._cleanup_task = None
        await scheduler.cleanup()
        tracer.close()


@pytest.mark.asyncio
async def test_shutdown_during_workspace_creation_rolls_back_new_role(tmp_path, monkeypatch):
    scheduler, tracer = _scheduler(tmp_path, prebuild_team=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    acquire = scheduler._worktree_pool.acquire

    async def delayed(role):
        entered.set()
        await release.wait()
        return await acquire(role)

    monkeypatch.setattr(scheduler._worktree_pool, "acquire", delayed)
    prebuild = asyncio.create_task(scheduler.ensure_team_prebuilt())
    try:
        await entered.wait()
        await scheduler.cleanup()
        release.set()
        with pytest.raises(RuntimeError, match="shutting down"):
            await prebuild
        assert _roles(scheduler) == {0: "analyst"}
        assert scheduler._worktree_pool._envs == []
    finally:
        release.set()
        await asyncio.gather(prebuild, return_exceptions=True)
        scheduler._cleanup_task = None
        await scheduler.cleanup()
        tracer.close()
