"""Lifecycle for per-spawn WorktreeEnvironments.

A spawned agent running in parallel needs its own physical workspace so it
cannot corrupt a sibling's edits. WorktreePool encapsulates: create a worktree
env for a given role, remember it for cleanup, tear them all down at the end.
"""

from __future__ import annotations

import logging
import uuid

from opencollab.adapters.env import (
    Environment,
    LocalEnvironment,
    WorktreeEnvironment,
)
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.domain.identity import role_storage_slug, validate_role_identity

logger = logging.getLogger(__name__)


async def _finish_cleanup(operation):
    return await await_owned_operation(operation, propagate_cancellation=True)


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
        """Create (and remember) an isolated env for a spawned agent of this role."""
        role = validate_role_identity(role)
        if not self._use_worktrees:
            return LocalEnvironment(self._workspace)

        branch = f"opencollab-{role_storage_slug(role)}-{uuid.uuid4().hex[:8]}"
        env = WorktreeEnvironment(self._workspace, branch_name=branch)
        try:
            await env.setup()
        except BaseException as original:
            try:
                await _finish_cleanup(env.cleanup())
            except BaseException as cleanup_exc:
                self._envs.append(env)
                logger.warning("partial worktree cleanup failed", exc_info=True)
                add_exception_note(
                    original,
                    "partial worktree retained for cleanup retry: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                )
            raise original
        self._envs.append(env)
        return env

    async def release(self) -> None:
        """Tear down every worktree this pool has handed out.

        One failing teardown must not abort the others, so each is isolated;
        the failure is logged rather than swallowed so it is diagnosable.
        """
        await _finish_cleanup(self._release_owned())

    async def _release_owned(self) -> None:
        failures: list[str] = []
        for env in tuple(self._envs):
            try:
                await _finish_cleanup(env.cleanup())
            except BaseException as exc:
                failures.append(f"{env.workspace}: {type(exc).__name__}: {exc}")
                logger.warning("worktree cleanup failed for %s", env.workspace, exc_info=True)
            else:
                self._envs.remove(env)
        if failures:
            raise OSError(
                "worktree pool cleanup failed; retry state retained: " + "; ".join(failures)
            )

    async def release_env(self, env: Environment) -> None:
        """Release one failed spawn's environment without touching siblings."""
        if not isinstance(env, WorktreeEnvironment) or env not in self._envs:
            return
        try:
            await _finish_cleanup(env.cleanup())
        except BaseException:
            logger.warning("worktree cleanup failed for %s", env.workspace, exc_info=True)
            raise
        self._envs.remove(env)

    async def cleanup(self) -> None:
        """Compatibility alias for older callers."""
        await self.release()
