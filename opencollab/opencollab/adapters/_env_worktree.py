"""Git-worktree-backed isolated execution environment."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Callable

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import WORKTREE_GIT_TIMEOUT_SECONDS
from opencollab.adapters._env_file_io import (
    _await_owned_transaction,
    _positive_finite_timeout,
)
from opencollab.adapters._env_git_repository import _PinnedGitRepository, trusted_git_command
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
    _ThreadProcessResult,
)
from opencollab.adapters._env_worktree_lifecycle import _WorktreeLifecycleMixin
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.application.exception_notes import add_exception_note

logger = logging.getLogger(__name__)


class WorktreeEnvironment(_WorktreeLifecycleMixin, Environment):
    """Git-worktree isolation for parallel spawned-agent execution."""

    local_filesystem = True

    def __init__(self, source_workspace: str, branch_name: str | None = None):
        self._source = os.path.realpath(os.path.abspath(source_workspace))
        self.source_workspace = self._source
        self._source_handle = _PinnedGitRepository(self._source)
        self._branch = branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        self._worktree_dir: str | None = None
        self._worktree_dir_fd = -1
        self._worktree_quarantine_dir: str | None = None
        self._worktree_directory_removed = True
        self._local_env: LocalEnvironment | None = None
        self._base_commit: str | None = None
        self._worktree_registered = False
        self._worktree_add_attempted = False
        self._branch_preexisting: bool | None = None
        self._branch_cleanup_pending = False
        self._branch_owned_oid: str | None = None
        self._add_owner_active = False

    def _sync_run_git(
        self,
        *args: str,
    ) -> tuple[int, bytes, bytes, bool]:
        return self._source_handle.run_cleanup(
            *args,
            timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
        )

    async def _run_git(
        self,
        *args: str,
        late_compensation: Callable[[_ThreadProcessResult], None] | None = None,
    ) -> ExecResult:
        timeout = _positive_finite_timeout(
            WORKTREE_GIT_TIMEOUT_SECONDS,
            name="WORKTREE_GIT_TIMEOUT_SECONDS",
        )
        operation_fd = self._source_handle.acquire()
        try:
            try:
                result = await _run_thread_owned_process(
                    trusted_git_command(*args),
                    shell=False,
                    cwd=None,
                    cwd_fd=operation_fd,
                    timeout=timeout,
                    timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    late_compensation=late_compensation,
                )
            except _OwnedProcessNotQuiesced:
                self._aborted = True
                raise
            except _OwnedProcessTimeout as exc:
                if not exc.cleanup_quiesced:
                    self._aborted = True
                raise RuntimeError(
                    f"git {' '.join(args)} timed out after {timeout:g}s; "
                    f"cleanup_quiesced={exc.cleanup_quiesced}"
                ) from exc
            except asyncio.CancelledError as exc:
                if getattr(exc, "cleanup_quiesced", True) is False:
                    self._aborted = True
                raise
        except BaseException as original:
            self._source_handle.finish(operation_fd, original)
            raise
        self._source_handle.finish(operation_fd)
        return ExecResult(
            returncode=result.returncode or 0,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            stdout_truncated=result.stdout_dropped_bytes > 0,
            stderr_truncated=result.stderr_dropped_bytes > 0,
            stdout_dropped_bytes=result.stdout_dropped_bytes,
            stderr_dropped_bytes=result.stderr_dropped_bytes,
        )

    async def setup(self) -> str:
        self._ensure_active()
        try:
            return await self._setup()
        except BaseException as original:
            try:
                await _await_owned_operation(self.cleanup())
            except BaseException as cleanup_exc:
                detail = (
                    "worktree setup compensation failed: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
                logger.error(detail)
                add_exception_note(original, detail)
            raise original

    async def _probe_setup_repository(self) -> None:
        repo_probe = await self._run_git("rev-parse", "--git-dir")
        if repo_probe.stdout_truncated or repo_probe.stderr_truncated:
            raise RuntimeError("git repository probe output was truncated")
        if repo_probe.returncode != 0:
            detail = repo_probe.stderr.strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError("worktree isolation requires a Git repository" + suffix)

        branch_probe = await self._run_git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{self._branch}",
        )
        if branch_probe.returncode not in (0, 1):
            raise RuntimeError(
                f"cannot probe worktree branch: {branch_probe.stderr.strip()}"
            )
        self._branch_preexisting = branch_probe.returncode == 0
        if self._branch_preexisting:
            raise RuntimeError(
                f"git worktree add failed: branch {self._branch} already exists"
            )

        base_result = await self._run_git("rev-parse", "--verify", "HEAD^{commit}")
        if (
            base_result.returncode != 0
            or base_result.stdout_truncated
            or base_result.stderr_truncated
        ):
            raise RuntimeError("cannot resolve worktree base commit")
        self._base_commit = base_result.stdout.strip()

    def _compensate_branch_claim(self, result: _ThreadProcessResult) -> None:
        if result.returncode != 0 or not result.cleanup_quiesced:
            return
        returncode, _stdout, stderr, quiesced = self._sync_run_git(
            "update-ref",
            "-d",
            f"refs/heads/{self._branch}",
            self._base_commit or "",
        )
        if returncode != 0 or not quiesced:
            raise OSError(
                "cancelled branch claim could not be rolled back atomically: "
                + stderr.decode(errors="replace").strip()
            )

    async def _claim_worktree_branch(self) -> None:
        claimed = await self._run_git(
            "update-ref",
            f"refs/heads/{self._branch}",
            self._base_commit,
            "0" * 40,
            late_compensation=self._compensate_branch_claim,
        )
        if claimed.returncode != 0:
            self._branch_preexisting = True
            raise RuntimeError(
                f"git worktree add failed: branch {self._branch} already exists"
            )
        self._branch_preexisting = False
        self._branch_cleanup_pending = True

    async def _add_claimed_worktree(self) -> str:
        self._worktree_dir = os.path.realpath(
            tempfile.mkdtemp(prefix="opencollab-wt-")
        )
        self._worktree_directory_removed = False
        self._capture_worktree_directory_handle()
        self._worktree_add_attempted = True
        self._add_owner_active = True

        added = await self._run_git(
            "worktree",
            "add",
            self._worktree_dir,
            self._branch,
            late_compensation=lambda _result: self._late_compensate_worktree_add(),
        )
        self._add_owner_active = False
        if added.returncode != 0:
            detail = added.stderr.strip()
            await self._refresh_partial_worktree_ownership()
            raise RuntimeError(f"git worktree add failed: {detail}")
        if self._worktree_directory_state(self._worktree_dir) != "owned":
            raise OSError("worktree directory changed while git worktree add completed")
        self._worktree_registered = True
        self._branch_cleanup_pending = True
        self.workspace = self._worktree_dir
        self._local_env = LocalEnvironment(self._worktree_dir)
        return self._worktree_dir

    async def _setup(self) -> str:
        """Create the worktree. Returns the worktree directory path."""
        self._ensure_active()
        await self._probe_setup_repository()
        await self._claim_worktree_branch()
        return await self._add_claimed_worktree()

    async def get_diff(self) -> str:
        self._ensure_active()
        if not self._local_env:
            return ""
        if not self._base_commit:
            raise RuntimeError("worktree base commit is unavailable")
        retirements = await self._local_env.registered_retirement_paths()
        result = await self._local_env.exec_cmd(
            guarded_staged_diff_command(
                base_revision=self._base_commit,
                registered_retirement_paths=retirements,
            )
        )
        if result.stdout_truncated or result.stderr_truncated:
            raise RuntimeError("worktree diff exceeded capture limit")
        if result.returncode != 0:
            raise RuntimeError("worktree diff extraction failed")
        return result.stdout

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.exec_cmd(cmd, timeout)

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        await self._local_env.write_file(path, content)

    async def registered_retirement_paths(self) -> tuple[str, ...]:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.registered_retirement_paths()

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if not self._local_env:
            await self.setup()
        return await self._local_env.write_temp_file(
            content,
            prefix=prefix,
            suffix=suffix,
        )

    async def remove_file(self, path: str) -> None:
        if self._local_env is not None:
            await self._local_env.remove_file(path)

    async def abort(self) -> None:
        await _await_owned_transaction(
            self._abort_worktree_resources(),
            failure_note="worktree abort",
        )

    async def _abort_worktree_resources(self) -> None:
        await super().abort()
        if self._local_env is not None:
            await self._local_env.abort()
        await self._cleanup_worktree_resources(raise_on_error=True)

    async def cleanup(self) -> None:
        await _await_owned_transaction(
            self._cleanup_worktree_resources(raise_on_error=True),
            failure_note="worktree cleanup",
        )
