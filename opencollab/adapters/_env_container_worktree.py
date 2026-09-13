"""A Git worktree carved out inside an already-running task container."""

from __future__ import annotations

import logging
import posixpath
import uuid
from collections.abc import Callable

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.adapters.git_worktree_evidence import (
    ABSENT_REF_OLD_VALUE,
    parse_own_commits,
    select_diff_base,
    validate_worktree_branch,
)

logger = logging.getLogger(__name__)

CONTAINER_GIT_TIMEOUT_SECONDS = 60.0


class ContainerWorktreeEnvironment(DockerEnvironment):
    """One agent's own checkout of the task repository, inside the container.

    The host-side twin, ``WorktreeEnvironment``, makes its worktree on the host
    file system. That is unusable where the repository under test exists only
    inside a container: reaching it from the host would take a bind mount, and a
    mount is exactly what a harness that extracts its patch from a bounded
    container archive cannot allow. This puts the worktree on the far side of
    the same boundary instead -- every Git command runs through ``docker exec``
    -- so the repository never has to be exposed to the host at all.

    Worktrees are placed outside the repository root on purpose. An archive of
    the repository root is what the surrounding harness reads the final patch
    out of, and a worktree nested inside it would arrive there as a directory
    full of files nobody wrote.

    Unlike the host twin this does not refuse a repository root with
    uncommitted changes. There the root is a workspace handed in from outside
    and dirt signals a setup mistake; here it is also an agent's own workspace,
    so refusing would fail a teammate's seat for no reason other than that a
    sibling had started working. The worktree is a clean checkout of the root's
    HEAD either way, which is the isolation this class exists to provide: work
    an agent has not committed is not in anyone else's tree.

    The evidence it reports is the same as the host twin's -- ``get_diff`` plus
    ``diff_base``, ``head_commit``, ``own_commits`` and ``own_commit_count`` --
    because that is the whole surface the scheduler's ``worktree_changes``
    record reads, and a handoff must join across the two the same way in either
    place.
    """

    def __init__(
        self,
        *,
        container_id: str,
        repository_root: str,
        worktree_root: str,
        branch_name: str | None = None,
        command_prefix: Callable[[str], str] | str | None = None,
        timeout_returncode: int = -1,
    ) -> None:
        branch = validate_worktree_branch(
            branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        )
        repository_root = _absolute_container_path(repository_root, "repository root")
        worktree_root = _absolute_container_path(worktree_root, "worktree root")
        worktree_dir = posixpath.join(worktree_root, branch)
        super().__init__(
            workspace=worktree_dir,
            container_id=container_id,
            exec_workdir=worktree_dir,
            command_prefix=command_prefix,
            timeout_returncode=timeout_returncode,
        )
        self.source_workspace = repository_root
        self._repository_root = repository_root
        self._worktree_root = worktree_root
        self._worktree_dir = worktree_dir
        self._branch = branch
        self._branch_owned = False
        self._worktree_registered = False
        self._base_commit: str | None = None
        self._diff_base: str | None = None
        self._head_commit: str | None = None
        self._own_commits: tuple[str, ...] = ()
        self._own_commit_count: int | None = None

    @property
    def diff_base(self) -> str | None:
        """The revision the last ``get_diff`` measured against, if any."""
        return self._diff_base

    @property
    def head_commit(self) -> str | None:
        """Where HEAD stood at that same reading, if it could be read."""
        return self._head_commit

    @property
    def own_commits(self) -> tuple[str, ...]:
        """The commits this worktree made since ``diff_base``, newest first."""
        return self._own_commits

    @property
    def own_commit_count(self) -> int | None:
        """How many commits ``own_commits`` was cut from, or ``None``."""
        return self._own_commit_count

    async def _git(self, workdir: str, *args: str) -> ExecResult:
        """Run one Git command in the container, outside the agent's shell.

        Not ``exec_cmd``: that runs what the agent's session runs, through the
        image's login shell and whatever command prefix the harness set. The
        worktree's own bookkeeping has to be argv-exact and independent of that.
        """
        await self._bind_attached()
        container_id = self._container_id
        if container_id is None:
            raise RuntimeError("container worktree is not attached to a container")
        result = await self._docker(
            "exec",
            "-w",
            workdir,
            "--",
            container_id,
            "git",
            "-c",
            f"safe.directory={workdir}",
            *args,
            timeout=CONTAINER_GIT_TIMEOUT_SECONDS,
        )
        return result.to_exec_result()

    async def setup(self, mount_dir: str | None = None) -> str:
        await super().setup(mount_dir)
        self._ensure_active()
        await self._create_worktree()
        return self._worktree_dir

    async def _create_worktree(self) -> None:
        made = await self._docker(
            "exec",
            "--",
            self._container_id or "",
            "mkdir",
            "-p",
            "--",
            self._worktree_root,
            timeout=CONTAINER_GIT_TIMEOUT_SECONDS,
        )
        if made.returncode != 0:
            raise RuntimeError("cannot create the container worktree root")
        base = await self._git(self._repository_root, "rev-parse", "--verify", "HEAD^{commit}")
        if base.returncode != 0 or base.stdout_truncated:
            raise RuntimeError("cannot resolve container worktree base commit")
        self._base_commit = base.stdout.strip()
        # The claimed ref is an ownership lease, not the worktree's HEAD: a
        # detached worktree keeps its own commits from moving that lease, so a
        # later external advance is observable and cannot be deleted by us.
        claimed = await self._git(
            self._repository_root,
            "update-ref",
            f"refs/heads/{self._branch}",
            self._base_commit,
            ABSENT_REF_OLD_VALUE,
        )
        if claimed.returncode != 0:
            raise RuntimeError(
                f"cannot atomically claim container worktree branch: {claimed.stderr.strip()}"
            )
        self._branch_owned = True
        added = await self._git(
            self._repository_root,
            "worktree",
            "add",
            "--detach",
            self._worktree_dir,
            self._base_commit,
        )
        if added.returncode != 0:
            raise RuntimeError(f"container git worktree add failed: {added.stderr.strip()}")
        self._worktree_registered = True

    async def get_diff(self) -> str:
        """This worktree's changes since the point its current work started from."""
        self._ensure_active()
        if self._base_commit is None:
            raise RuntimeError("container worktree base commit is unavailable")
        self._diff_base = await self._resolve_diff_base()
        await self._resolve_own_commits(self._diff_base)
        result = await self.exec_cmd(
            guarded_staged_diff_command(base_revision=self._diff_base)
        )
        if result.stdout_truncated or result.stderr_truncated:
            raise RuntimeError(
                f"container worktree diff exceeded capture limit at {self._worktree_dir}"
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"git exited with status {result.returncode}"
            raise RuntimeError(
                f"container worktree diff extraction failed: {detail}; "
                f"worktree retained at {self._worktree_dir}"
            )
        return result.stdout

    async def _resolve_diff_base(self) -> str:
        assert self._base_commit is not None
        reflog = await self._git(
            self._worktree_dir, "log", "-g", "--format=%H%x09%gs", "HEAD"
        )
        if reflog.returncode != 0 or reflog.stdout_truncated:
            return self._base_commit
        return select_diff_base(reflog.stdout, fallback=self._base_commit)

    async def _resolve_own_commits(self, base_revision: str) -> None:
        self._head_commit = None
        self._own_commits = ()
        self._own_commit_count = None
        head = await self._git(self._worktree_dir, "rev-parse", "HEAD")
        if head.returncode != 0 or head.stdout_truncated:
            return
        self._head_commit = head.stdout.strip() or None
        listed = await self._git(
            self._worktree_dir, "rev-list", f"{base_revision}..HEAD"
        )
        if listed.returncode != 0 or listed.stdout_truncated:
            return
        self._own_commits, self._own_commit_count = parse_own_commits(listed.stdout)

    async def cleanup(self) -> None:
        """Remove the worktree and release its branch lease, then detach.

        Every step is best effort and reported rather than raised: the container
        is the harness's to tear down, and a worktree left behind inside one
        that is about to be removed costs nothing, while a raising cleanup would
        turn a finished run into a failed one.
        """
        if self._worktree_registered:
            removed = await self._git(
                self._repository_root, "worktree", "remove", "--force", self._worktree_dir
            )
            if removed.returncode != 0:
                logger.warning(
                    "container worktree not removed at %s: %s",
                    self._worktree_dir,
                    removed.stderr.strip(),
                )
            else:
                self._worktree_registered = False
        if self._branch_owned and self._base_commit is not None:
            # Delete only a lease that still stands where it was claimed, so a
            # branch something else advanced is left for its owner to explain.
            released = await self._git(
                self._repository_root,
                "update-ref",
                "-d",
                f"refs/heads/{self._branch}",
                self._base_commit,
            )
            if released.returncode == 0:
                self._branch_owned = False
            else:
                logger.warning(
                    "container worktree branch lease retained: %s", released.stderr.strip()
                )
        await super().cleanup()


def _absolute_container_path(path: str, label: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError(f"container {label} must be an absolute path without NUL bytes")
    normalized = posixpath.normpath(path)
    if normalized == "/":
        raise ValueError(f"container {label} must not be the container root")
    return normalized


__all__ = ["CONTAINER_GIT_TIMEOUT_SECONDS", "ContainerWorktreeEnvironment"]
