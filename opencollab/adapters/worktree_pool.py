"""Lifecycle for per-spawn WorktreeEnvironments.

A spawned agent running in parallel needs its own physical workspace so it
cannot corrupt a sibling's edits. WorktreePool encapsulates: create a worktree
env for a given role, remember it for cleanup, tear them all down at the end.
"""

from __future__ import annotations

import logging
import uuid

from opencollab.adapters.env import (
    ContainerWorktreeEnvironment,
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    WorktreeEnvironment,
)
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.domain.identity import role_storage_slug, validate_role_identity

logger = logging.getLogger(__name__)

# Where per-agent checkouts go inside a container. Deliberately not under the
# repository root: an archive of that root is what a harness reads the run's
# result out of, and a worktree nested in it would arrive there as a directory
# full of files no agent wrote.
CONTAINER_WORKTREE_ROOT = "/opencollab-worktrees"


async def _finish_cleanup(operation):
    return await await_owned_operation(operation, propagate_cancellation=True)


class WorktreePool:
    """Lends out worktree-isolated environments and tracks them for cleanup.

    When use_worktrees is False, hands out LocalEnvironment(workspace) instead
    — caller code does not need to branch on the mode.

    ``base_environment`` says where the repository being worked on actually
    lives. Left unset, it is the host file system and a worktree is made there.
    Set to a container this run is attached to, every worktree is carved out
    inside that container instead, because a repository that exists only there
    cannot be reached from the host without a mount — and a harness that reads
    its result out of a bounded container archive is precisely one that cannot
    allow a mount. Either way the agents get the same isolation and report the
    same evidence; only the side of the wall changes.
    """

    def __init__(
        self,
        workspace: str,
        *,
        use_worktrees: bool,
        base_environment: Environment | None = None,
    ):
        self._workspace = workspace
        self._use_worktrees = use_worktrees
        self._base_environment = base_environment
        self._envs: list[Environment] = []

    async def acquire(self, role: str) -> Environment:
        """Create (and remember) an isolated env for a spawned agent of this role."""
        role = validate_role_identity(role)
        base = self._base_environment
        if not self._use_worktrees:
            # Without isolation every agent works where the run works, which is
            # the supplied environment when there is one and the host workspace
            # otherwise. The shared environment is not tracked for cleanup here:
            # it belongs to whoever handed it in.
            if base is not None:
                return base
            env = LocalEnvironment(self._workspace)
            self._envs.append(env)
            return env

        branch = f"opencollab-{role_storage_slug(role)}-{uuid.uuid4().hex[:8]}"
        env = self._build_worktree(branch, base)
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

    def _build_worktree(self, branch: str, base: Environment | None) -> Environment:
        """An isolated view of wherever the run's repository actually is."""
        if base is None:
            return WorktreeEnvironment(self._workspace, branch_name=branch)
        if isinstance(base, DockerEnvironment) and base.container_reference is not None:
            return ContainerWorktreeEnvironment(
                container_id=base.container_reference,
                repository_root=base.workspace,
                worktree_root=CONTAINER_WORKTREE_ROOT,
                branch_name=branch,
                command_prefix=base.command_prefix,
            )
        if getattr(base, "local_filesystem", False):
            return WorktreeEnvironment(base.workspace, branch_name=branch)
        raise TypeError(
            "worktree isolation is not available for this environment: "
            f"{type(base).__name__}"
        )

    async def release(self) -> None:
        """Tear down every environment this pool has handed out.

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
                logger.warning(
                    "environment cleanup failed for %s",
                    env.workspace,
                    exc_info=True,
                )
            else:
                self._envs.remove(env)
        if failures:
            raise OSError(
                "environment pool cleanup failed; retry state retained: "
                + "; ".join(failures)
            )

    async def release_env(self, env: Environment) -> None:
        """Release one failed spawn's environment without touching siblings."""
        if env not in self._envs:
            return
        try:
            await _finish_cleanup(env.cleanup())
        except BaseException:
            logger.warning(
                "environment cleanup failed for %s", env.workspace, exc_info=True
            )
            raise
        self._envs.remove(env)

    async def cleanup(self) -> None:
        """Compatibility alias for older callers."""
        await self.release()
