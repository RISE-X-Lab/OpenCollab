"""Git worktree environment with a non-Git copy fallback."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import tempfile
import uuid

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import run_process
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note

WORKTREE_GIT_TIMEOUT_SECONDS = 30.0
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
_ZERO_OID = "0" * 40


class WorktreeEnvironment(Environment):
    """Give one agent an isolated Git worktree or copied plain directory."""

    local_filesystem = True
    process_isolated = False

    def __init__(self, source_workspace: str, branch_name: str | None = None) -> None:
        super().__init__()
        self._source = os.path.realpath(os.path.abspath(source_workspace))
        if not os.path.isdir(self._source):
            raise NotADirectoryError(self._source)
        self.source_workspace = self._source
        self.host_workspace = None
        self._branch = branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        if not _BRANCH_RE.fullmatch(self._branch):
            raise ValueError("worktree branch name must be one safe Git ref component")
        self._worktree_dir: str | None = None
        self._copy_baseline_dir: str | None = None
        self._copy_exported_diff: str | None = None
        self._local_env: LocalEnvironment | None = None
        self._base_commit: str | None = None
        self._git_mode = False
        self._branch_owned = False
        self._owned_branch_oid: str | None = None
        self._worktree_registered = False
        self._lifecycle_lock = asyncio.Lock()

    async def _git(self, *args: str, timeout: float = WORKTREE_GIT_TIMEOUT_SECONDS) -> ExecResult:
        result = await run_process(
            ("git", *args),
            shell=False,
            cwd=self._source,
            timeout=timeout,
        )
        return result.to_exec_result()

    async def setup(self, mount_dir: str | None = None) -> str:
        async with self._lifecycle_lock:
            return await self._setup_locked(mount_dir)

    async def _setup_locked(self, mount_dir: str | None) -> str:
        self._ensure_active()
        if mount_dir is not None:
            raise ValueError("mount_dir is supported only by container environments")
        if self._local_env is not None:
            return self.workspace
        probe = await self._git("rev-parse", "--git-dir")
        if probe.stdout_truncated or probe.stderr_truncated:
            raise RuntimeError("git repository probe output was truncated")
        self._git_mode = probe.returncode == 0
        try:
            if self._git_mode:
                await self._setup_git_worktree()
            else:
                await self._setup_directory_copy()
        except BaseException as original:
            try:
                await await_owned_operation(
                    self._cleanup_resources(),
                    propagate_cancellation=not isinstance(
                        original,
                        asyncio.CancelledError,
                    ),
                )
            except asyncio.CancelledError:
                if not isinstance(original, asyncio.CancelledError):
                    raise
                raise original
            except BaseException as cleanup_error:
                add_exception_note(
                    original,
                    "worktree setup cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise
        assert self._worktree_dir is not None
        self.workspace = self._worktree_dir
        self.host_workspace = self._worktree_dir
        self._local_env = LocalEnvironment(self._worktree_dir)
        return self._worktree_dir

    async def _setup_git_worktree(self) -> None:
        branch_probe = await self._git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{self._branch}",
        )
        if branch_probe.returncode == 0:
            raise RuntimeError(f"git worktree branch already exists: {self._branch}")
        if branch_probe.returncode != 1:
            raise RuntimeError(f"cannot probe worktree branch: {branch_probe.stderr.strip()}")
        base = await self._git("rev-parse", "--verify", "HEAD^{commit}")
        if base.returncode != 0 or base.stdout_truncated or base.stderr_truncated:
            raise RuntimeError("cannot resolve worktree base commit")
        self._base_commit = base.stdout.strip()
        claimed = await self._git(
            "update-ref",
            f"refs/heads/{self._branch}",
            self._base_commit,
            _ZERO_OID,
        )
        if claimed.returncode != 0:
            branch_probe = await self._git(
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{self._branch}",
            )
            if branch_probe.returncode == 0:
                raise RuntimeError(f"git worktree branch already exists: {self._branch}")
            raise RuntimeError(f"cannot atomically claim worktree branch: {claimed.stderr.strip()}")
        self._branch_owned = True
        self._owned_branch_oid = self._base_commit
        self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-wt-")
        # The claimed ref is an ownership lease, not the worktree's HEAD. A
        # detached worktree keeps its own commits from moving that lease, so a
        # later external ref advance is observable and cannot be deleted by us.
        added = await self._git(
            "worktree",
            "add",
            "--detach",
            self._worktree_dir,
            self._base_commit,
        )
        if added.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {added.stderr.strip()}")
        self._worktree_registered = True

    async def _setup_directory_copy(self) -> None:
        self._copy_baseline_dir = tempfile.mkdtemp(prefix="opencollab-cp-baseline-")
        self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-cp-")
        await await_owned_operation(
            asyncio.to_thread(
                shutil.copytree,
                self._source,
                self._copy_baseline_dir,
                dirs_exist_ok=True,
                symlinks=True,
            ),
            propagate_cancellation=True,
        )
        await await_owned_operation(
            asyncio.to_thread(
                shutil.copytree,
                self._source,
                self._worktree_dir,
                dirs_exist_ok=True,
                symlinks=True,
            ),
            propagate_cancellation=True,
        )

    async def _delete_owned_branch(self, expected_oid: str | None) -> None:
        if not expected_oid:
            return
        deleted = await self._git(
            "update-ref",
            "-d",
            f"refs/heads/{self._branch}",
            expected_oid,
        )
        if deleted.returncode != 0:
            raise RuntimeError(f"cannot delete owned worktree branch: {deleted.stderr.strip()}")
        self._branch_owned = False
        self._owned_branch_oid = None

    async def get_diff(self) -> str:
        self._ensure_active()
        if self._local_env is None:
            return ""
        if not self._git_mode:
            diff = await self._directory_copy_diff()
            self._copy_exported_diff = diff
            return diff
        if self._base_commit is None:
            raise RuntimeError("worktree base commit is unavailable")
        result = await self._local_env.exec_cmd(
            guarded_staged_diff_command(base_revision=self._base_commit)
        )
        if result.stdout_truncated or result.stderr_truncated:
            raise RuntimeError("worktree diff exceeded capture limit")
        if result.returncode != 0:
            detail = result.stderr.strip() or f"git exited with status {result.returncode}"
            raise RuntimeError(f"worktree diff extraction failed: {detail}")
        return result.stdout

    async def _directory_copy_diff(self) -> str:
        if self._copy_baseline_dir is None or self._worktree_dir is None:
            raise RuntimeError("non-Git worktree baseline is unavailable")
        command = (
            "git diff --no-index --binary --no-ext-diff -- "
            f"{shlex.quote(self._copy_baseline_dir)} {shlex.quote(self._worktree_dir)}"
        )
        assert self._local_env is not None
        result = await self._local_env.exec_cmd(command)
        if result.stdout_truncated or result.stderr_truncated:
            raise RuntimeError("non-Git worktree diff exceeded capture limit")
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or f"git exited with status {result.returncode}"
            raise RuntimeError(f"non-Git worktree diff extraction failed: {detail}")
        return (
            result.stdout
            .replace(f"a{self._copy_baseline_dir}/", "a/")
            .replace(f"a{self._worktree_dir}/", "a/")
            .replace(f"b{self._copy_baseline_dir}/", "b/")
            .replace(f"b{self._worktree_dir}/", "b/")
        )

    async def _ensure_directory_copy_changes_exported(self) -> None:
        if self._git_mode or self._copy_baseline_dir is None or self._worktree_dir is None:
            return
        current = await self._directory_copy_diff()
        if current != (self._copy_exported_diff or ""):
            raise RuntimeError(
                "refusing to clean unexported non-Git worktree changes; "
                "call get_diff() and deliver its artifact first"
            )

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure_active()
        if self._local_env is None:
            await self.setup()
        assert self._local_env is not None
        return await self._local_env.exec_cmd(cmd, timeout)

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        if self._local_env is None:
            await self.setup()
        assert self._local_env is not None
        return await self._local_env.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        if self._local_env is None:
            await self.setup()
        assert self._local_env is not None
        await self._local_env.write_file(path, content)

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if self._local_env is None:
            await self.setup()
        assert self._local_env is not None
        return await self._local_env.write_temp_file(content, prefix=prefix, suffix=suffix)

    async def remove_file(self, path: str) -> None:
        if self._local_env is not None:
            await self._local_env.remove_file(path)

    async def _cleanup_resources(self) -> None:
        failures: list[BaseException] = []
        if self._local_env is not None:
            try:
                await self._local_env.cleanup()
            except BaseException as exc:
                failures.append(exc)
            else:
                self._local_env = None
        if self._git_mode and self._worktree_registered and self._worktree_dir is not None:
            removed = await self._git("worktree", "remove", "--force", self._worktree_dir)
            if removed.returncode != 0:
                failures.append(RuntimeError(f"git worktree remove failed: {removed.stderr.strip()}"))
            else:
                self._worktree_registered = False
        if not self._worktree_registered and self._worktree_dir is not None:
            try:
                await asyncio.to_thread(shutil.rmtree, self._worktree_dir)
            except FileNotFoundError:
                self._worktree_dir = None
            except BaseException as exc:
                failures.append(exc)
            else:
                self._worktree_dir = None
        if self._worktree_dir is None and self._copy_baseline_dir is not None:
            try:
                await asyncio.to_thread(shutil.rmtree, self._copy_baseline_dir)
            except FileNotFoundError:
                self._copy_baseline_dir = None
            except BaseException as exc:
                failures.append(exc)
            else:
                self._copy_baseline_dir = None
        if self._git_mode and self._branch_owned and self._worktree_dir is None:
            expected_oid = self._owned_branch_oid
            if expected_oid is None:
                self._branch_owned = False
            else:
                branch = await self._git("rev-parse", f"refs/heads/{self._branch}")
                if branch.returncode == 0:
                    if branch.stdout.strip() == expected_oid:
                        try:
                            await self._delete_owned_branch(expected_oid)
                        except BaseException as exc:
                            failures.append(exc)
                    else:
                        self._branch_owned = False
                        self._owned_branch_oid = None
                elif branch.returncode == 128:
                    self._branch_owned = False
                    self._owned_branch_oid = None
                else:
                    failures.append(RuntimeError("cannot inspect owned worktree branch"))
        if failures:
            raise RuntimeError("worktree cleanup failed") from failures[0]

    async def cleanup(self) -> None:
        async with self._lifecycle_lock:
            await self._ensure_directory_copy_changes_exported()
            self.revoke()
            await await_owned_operation(
                self._cleanup_resources(),
                propagate_cancellation=True,
            )

    async def abort(self) -> None:
        async with self._lifecycle_lock:
            self.revoke()
            await self._ensure_directory_copy_changes_exported()
            await await_owned_operation(
                self._cleanup_resources(),
                propagate_cancellation=True,
            )

__all__ = ["WorktreeEnvironment"]
