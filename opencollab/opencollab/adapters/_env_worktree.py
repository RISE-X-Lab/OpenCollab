"""Git-worktree-backed isolated execution environment."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Callable

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import WORKTREE_GIT_TIMEOUT_SECONDS
from opencollab.adapters._env_file_io import (
    _await_owned_transaction,
    _positive_finite_timeout,
    _run_owned_blocking_io,
)
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
    _sync_run_cleanup_command,
    _ThreadProcessResult,
)

logger = logging.getLogger(__name__)


class WorktreeEnvironment(Environment):
    """Isolated git worktree — for parallel spawned-agent execution.

    Each spawned agent gets a separate physical copy of the repo via
    `git worktree add`. After task completion, changes are collected as a
    diff patch.

    This solves the concurrency problem: spawned agents cannot corrupt each
    other's git state, env variables, or file locks.

    Ref: User feedback on blind spot #2 — parallel delegation must use
    separate physical workspaces, not just file locks.
    """

    local_filesystem = True

    def __init__(self, source_workspace: str, branch_name: str | None = None):
        self._source = os.path.abspath(source_workspace)
        self.source_workspace = self._source
        # Use UUID to guarantee uniqueness even under parallel same-role delegation
        self._branch = branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        self._worktree_dir: str | None = None
        self._local_env: LocalEnvironment | None = None
        self._base_commit: str | None = None
        self._worktree_registered = False
        self._worktree_add_attempted = False
        self._branch_preexisting: bool | None = None
        self._branch_cleanup_pending = False
        self._add_owner_active = False

    async def _run_git(
        self,
        *args: str,
        late_compensation: Callable[["_ThreadProcessResult"], None] | None = None,
    ) -> ExecResult:
        timeout = _positive_finite_timeout(
            WORKTREE_GIT_TIMEOUT_SECONDS,
            name="WORKTREE_GIT_TIMEOUT_SECONDS",
        )
        try:
            result = await _run_thread_owned_process(
                ["git", *args],
                shell=False,
                cwd=self._source,
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
                f"git {' '.join(args)} timed out after {timeout:g}s; cleanup_quiesced={exc.cleanup_quiesced}"
            ) from exc
        except asyncio.CancelledError as exc:
            if getattr(exc, "cleanup_quiesced", True) is False:
                self._aborted = True
            raise
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
                detail = f"worktree setup compensation failed: {type(cleanup_exc).__name__}: {cleanup_exc}"
                logger.error(detail)
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(detail)
            raise original

    async def _setup(self) -> str:
        """Create the worktree. Returns the worktree directory path."""
        self._ensure_active()
        repo_probe = await self._run_git("rev-parse", "--git-dir")
        if repo_probe.stdout_truncated or repo_probe.stderr_truncated:
            raise RuntimeError("git repository probe output was truncated")
        if repo_probe.returncode != 0:
            detail = repo_probe.stderr.strip()
            raise RuntimeError("worktree isolation requires a Git repository" + (f": {detail}" if detail else ""))

        branch_probe = await self._run_git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{self._branch}",
        )
        if branch_probe.returncode not in (0, 1):
            raise RuntimeError(f"cannot probe worktree branch: {branch_probe.stderr.strip()}")
        self._branch_preexisting = branch_probe.returncode == 0
        if self._branch_preexisting:
            raise RuntimeError(f"git worktree add failed: branch {self._branch} already exists")

        base = await self._run_git("rev-parse", "HEAD")
        if base.stdout_truncated or base.stderr_truncated:
            raise RuntimeError("git base commit output was truncated")
        if base.returncode != 0:
            raise RuntimeError(f"cannot record worktree base commit: {base.stderr.strip()}")
        self._base_commit = base.stdout.strip()

        def compensate_claim(result: _ThreadProcessResult) -> None:
            if result.returncode != 0 or not result.cleanup_quiesced:
                return
            returncode, _stdout, stderr, quiesced = _sync_run_cleanup_command(
                [
                    "git",
                    "update-ref",
                    "-d",
                    f"refs/heads/{self._branch}",
                    self._base_commit or "",
                ],
                cwd=self._source,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
            )
            if returncode != 0 or not quiesced:
                raise OSError(
                    "cancelled branch claim could not be rolled back atomically: "
                    + stderr.decode(errors="replace").strip()
                )

        claimed = await self._run_git(
            "update-ref",
            f"refs/heads/{self._branch}",
            self._base_commit,
            "0" * 40,
            late_compensation=compensate_claim,
        )
        if claimed.returncode != 0:
            self._branch_preexisting = True
            raise RuntimeError(f"git worktree add failed: branch {self._branch} already exists")
        self._branch_preexisting = False
        self._branch_cleanup_pending = True
        self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-wt-")
        self._worktree_add_attempted = True
        self._add_owner_active = True

        def compensate_add(_result: _ThreadProcessResult) -> None:
            self._late_compensate_worktree_add()

        added = await self._run_git(
            "worktree",
            "add",
            self._worktree_dir,
            self._branch,
            late_compensation=compensate_add,
        )
        self._add_owner_active = False
        if added.returncode != 0:
            detail = added.stderr.strip()
            await self._refresh_partial_worktree_ownership()
            raise RuntimeError(f"git worktree add failed: {detail}")
        self._worktree_registered = True
        self._branch_cleanup_pending = True

        self.workspace = self._worktree_dir
        self._local_env = LocalEnvironment(self._worktree_dir)
        return self._worktree_dir

    def _late_compensate_worktree_add(self) -> None:
        errors: list[str] = []
        try:
            worktree_dir = self._worktree_dir
            owned_branch = self._branch_cleanup_pending
            if worktree_dir:
                try:
                    returncode, stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "list", "--porcelain"],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    expected = os.path.realpath(worktree_dir)
                    registered = (
                        returncode == 0
                        and quiesced
                        and expected
                        in {
                            os.path.realpath(line.removeprefix("worktree "))
                            for line in stdout.decode(errors="replace").splitlines()
                            if line.startswith("worktree ")
                        }
                    )
                    if registered:
                        self._worktree_registered = True
                        owned_branch = self._branch_preexisting is False
                        self._branch_cleanup_pending = owned_branch
                    elif returncode != 0 or not quiesced:
                        errors.append("late worktree ownership probe was indeterminate")
                except BaseException as exc:
                    logger.error("late git worktree ownership probe failed: %s", exc)
                    errors.append(f"late worktree ownership probe failed: {exc}")
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    if returncode == 0 and quiesced:
                        self._worktree_registered = False
                    else:
                        self._worktree_registered = True
                        errors.append("late git worktree remove failed")
                except BaseException as exc:
                    logger.error("late git worktree compensation failed: %s", exc)
                    self._worktree_registered = True
                    errors.append(f"late git worktree remove failed: {exc}")
                if os.path.exists(worktree_dir):
                    shutil.rmtree(worktree_dir, ignore_errors=True)
                    if os.path.exists(worktree_dir):
                        errors.append(f"late worktree directory still exists: {worktree_dir}")
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        ["git", "worktree", "prune", "--expire", "now"],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                    if returncode != 0 or not quiesced:
                        errors.append("late git worktree prune failed")
                except BaseException as exc:
                    logger.error("late git worktree prune failed: %s", exc)
                    errors.append(f"late git worktree prune failed: {exc}")
            if owned_branch and self._branch_preexisting is False:
                try:
                    returncode, _stdout, _stderr, quiesced = _sync_run_cleanup_command(
                        [
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/heads/{self._branch}",
                        ],
                        cwd=self._source,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                        timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                    )
                except BaseException as exc:
                    logger.error("late git branch probe failed: %s", exc)
                    self._branch_cleanup_pending = True
                    errors.append(f"late git branch probe failed: {exc}")
                else:
                    if quiesced and returncode == 1:
                        self._branch_cleanup_pending = False
                    elif quiesced and returncode == 0:
                        try:
                            (
                                delete_returncode,
                                _stdout,
                                _stderr,
                                delete_quiesced,
                            ) = _sync_run_cleanup_command(
                                ["git", "branch", "-D", self._branch],
                                cwd=self._source,
                                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                                timeout_name="WORKTREE_GIT_TIMEOUT_SECONDS",
                            )
                        except BaseException as exc:
                            errors.append(f"late git branch delete failed: {exc}")
                            self._branch_cleanup_pending = True
                        else:
                            self._branch_cleanup_pending = not (delete_quiesced and delete_returncode == 0)
                            if self._branch_cleanup_pending:
                                errors.append("late git branch delete failed")
                    else:
                        self._branch_cleanup_pending = True
                        errors.append("late git branch probe was indeterminate")
        finally:
            self._add_owner_active = False
        if errors:
            raise OSError("; ".join(errors))

    async def _refresh_partial_worktree_ownership(self) -> None:
        if not self._worktree_dir:
            return
        listed = await self._run_git("worktree", "list", "--porcelain")
        if listed.returncode != 0:
            raise RuntimeError(f"cannot inspect worktree registration: {listed.stderr.strip()}")
        if listed.stdout_truncated:
            raise RuntimeError("git worktree list output was truncated")
        expected = os.path.realpath(self._worktree_dir)
        registered_paths = {
            os.path.realpath(line.removeprefix("worktree "))
            for line in listed.stdout.splitlines()
            if line.startswith("worktree ")
        }
        self._worktree_registered = expected in registered_paths
        if self._worktree_registered:
            if self._branch_preexisting is False:
                self._branch_cleanup_pending = True

    async def _cleanup_worktree_resources(self, *, raise_on_error: bool) -> None:
        errors: list[str] = []
        if self._add_owner_active:
            error = "git worktree add owner is still performing compensation"
            if raise_on_error:
                raise OSError(error)
            logger.error(error)
            return
        worktree_dir = self._worktree_dir
        ownership_unknown = False
        if worktree_dir and self._worktree_add_attempted:
            try:
                await self._refresh_partial_worktree_ownership()
            except BaseException as exc:
                errors.append(f"worktree ownership probe failed: {exc}")
                ownership_unknown = True

        if worktree_dir and (self._worktree_registered or ownership_unknown):
            try:
                removed = await self._run_git(
                    "worktree",
                    "remove",
                    "--force",
                    worktree_dir,
                )
            except BaseException as exc:
                errors.append(f"git worktree remove failed: {exc}")
            else:
                if removed.returncode == 0:
                    self._worktree_registered = False
                    ownership_unknown = False
                else:
                    self._worktree_registered = True
                    errors.append("git worktree remove failed: " + removed.stderr.strip())

        if worktree_dir and os.path.exists(worktree_dir):
            await _run_owned_blocking_io(
                shutil.rmtree,
                worktree_dir,
                True,
            )
            if os.path.exists(worktree_dir):
                errors.append(f"worktree directory still exists: {worktree_dir}")

        if worktree_dir and (self._worktree_registered or ownership_unknown):
            try:
                pruned = await self._run_git(
                    "worktree",
                    "prune",
                    "--expire",
                    "now",
                )
                if pruned.returncode != 0:
                    errors.append("git worktree prune failed: " + pruned.stderr.strip())
                else:
                    listed = await self._run_git("worktree", "list", "--porcelain")
                    if listed.stdout_truncated:
                        raise RuntimeError("git worktree list output was truncated")
                    if listed.returncode == 0 and os.path.realpath(worktree_dir) not in {
                        os.path.realpath(line.removeprefix("worktree "))
                        for line in listed.stdout.splitlines()
                        if line.startswith("worktree ")
                    }:
                        self._worktree_registered = False
                        ownership_unknown = False
            except BaseException as exc:
                errors.append(f"git worktree prune failed: {exc}")

        if self._branch_cleanup_pending:
            try:
                branch_probe = await self._run_git(
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{self._branch}",
                )
            except BaseException as exc:
                errors.append(f"branch cleanup probe failed: {exc}")
            else:
                if branch_probe.returncode == 1:
                    self._branch_cleanup_pending = False
                elif branch_probe.returncode != 0:
                    errors.append("branch cleanup probe failed: " + branch_probe.stderr.strip())
                else:
                    try:
                        deleted = await self._run_git("branch", "-D", self._branch)
                    except BaseException as exc:
                        errors.append(f"git branch -D failed: {exc}")
                    else:
                        if deleted.returncode == 0:
                            self._branch_cleanup_pending = False
                        else:
                            errors.append("git branch -D failed: " + deleted.stderr.strip())

        directory_gone = not worktree_dir or not os.path.exists(worktree_dir)
        if not self._worktree_registered and not ownership_unknown and directory_gone:
            self._worktree_dir = None
            self._local_env = None
        if (
            not self._branch_cleanup_pending
            and not self._worktree_registered
            and not ownership_unknown
            and directory_gone
        ):
            self._worktree_add_attempted = False
        if errors and raise_on_error:
            raise OSError("; ".join(errors))

    async def get_diff(self) -> str:
        """Get the diff of changes made in this worktree."""
        self._ensure_active()
        if not self._local_env:
            return ""
        if not self._base_commit:
            raise RuntimeError("worktree base commit is unavailable")
        # Intent-to-add makes untracked, non-ignored paths visible to git diff
        # without staging their contents. Comparing against the creation commit
        # also captures commits made by the child after the worktree was created.
        intent = await self._local_env.exec_cmd("git add --intent-to-add -- .")
        if intent.returncode != 0:
            raise RuntimeError(f"cannot enumerate untracked worktree files: {intent.stderr.strip()}")
        result = await self._local_env.exec_cmd(f"git diff --binary {shlex.quote(self._base_commit)} --")
        if result.returncode != 0:
            raise RuntimeError(f"cannot collect worktree diff: {result.stderr.strip()}")
        if result.stdout_truncated:
            raise RuntimeError("worktree diff exceeded capture limit")
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
        await super().abort()
        if self._local_env is not None:
            await self._local_env.abort()

    async def cleanup(self) -> None:
        """Remove worktree and temporary branch."""
        await _await_owned_transaction(
            self._cleanup_worktree_resources(raise_on_error=True),
            failure_note="worktree cleanup",
        )
