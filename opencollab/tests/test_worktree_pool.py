"""WorktreePool delegates to LocalEnvironment when worktrees are disabled,
and tracks WorktreeEnvironments for cleanup when enabled."""

from __future__ import annotations

import asyncio

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.worktree_pool import WorktreePool


def test_pool_returns_local_env_when_worktrees_disabled(tmp_path):
    pool = WorktreePool(str(tmp_path), use_worktrees=False)

    env1 = asyncio.run(pool.acquire("coder"))
    env2 = asyncio.run(pool.acquire("reviewer"))

    assert isinstance(env1, LocalEnvironment)
    assert isinstance(env2, LocalEnvironment)
    # No tracking — LocalEnvironments don't need cleanup
    assert pool._envs == []

    # cleanup() is a no-op; should not raise
    asyncio.run(pool.cleanup())
