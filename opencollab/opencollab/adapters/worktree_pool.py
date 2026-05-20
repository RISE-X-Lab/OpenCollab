"""Lifecycle for per-teammate WorktreeEnvironments.

A teammate running in parallel needs its own physical workspace so it cannot
corrupt a sibling's edits. WorktreePool encapsulates: create a worktree env
for a given role, remember it for cleanup, tear them all down at the end.
"""

from __future__ import annotations

import uuid

from opencollab.adapters.env import Environment, LocalEnvironment, WorktreeEnvironment


class WorktreePool:
    """Lends out worktree-isolated environments and tracks them for cleanup.

    When use_worktrees is False, hands out LocalEnvironment(workspace) instead
    — caller code does not need to branch on the mode.
    """

    def __init__(self, workspace: str, *, use_worktrees: bool):
        self._workspace = workspace
        self._use_worktrees = use_worktrees
        self._envs: list[WorktreeEnvironment] = []

    async def acquire(self, role: str) -> Environment:
        """Create (and remember) an isolated env for a teammate of this role."""
        if not self._use_worktrees:
            return LocalEnvironment(self._workspace)

        branch = f"opencollab-{role}-{uuid.uuid4().hex[:8]}"
        env = WorktreeEnvironment(self._workspace, branch_name=branch)
        await env.setup()
        self._envs.append(env)
        return env

    async def release(self) -> None:
        """Tear down every worktree this pool has handed out."""
        for env in self._envs:
            try:
                await env.cleanup()
            except Exception:
                pass
        self._envs.clear()

    async def cleanup(self) -> None:
        """Compatibility alias for older callers."""
        await self.release()
