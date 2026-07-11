"""Git-worktree-backed isolated execution environment."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import uuid
from collections.abc import Callable

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import WORKTREE_GIT_TIMEOUT_SECONDS
from opencollab.adapters._env_directory_cleanup import (
    _parse_object_id,
    _sync_clear_pinned_directory,
)
from opencollab.adapters._env_file_io import (
    _await_owned_transaction,
    _open_parent_dirfd,
    _positive_finite_timeout,
    _run_owned_blocking_io,
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
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.application.exception_notes import add_exception_note

logger = logging.getLogger(__name__)


class WorktreeEnvironment(Environment):
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
    def _capture_worktree_directory_handle(self) -> None:
        if self._worktree_dir_fd >= 0 or not self._worktree_dir:
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(self._worktree_dir, flags)
        try:
            opened = os.fstat(fd)
            current = os.lstat(self._worktree_dir)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise OSError("worktree directory changed while recording ownership")
        except BaseException:
            os.close(fd)
            raise
        self._worktree_dir_fd = fd
    def _worktree_directory_state(self, path: str) -> str:
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            return "absent"
        if self._worktree_dir_fd < 0:
            return "unverified"
        opened = os.fstat(self._worktree_dir_fd)
        if (
            stat.S_ISDIR(opened.st_mode)
            and stat.S_ISDIR(current.st_mode)
            and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
        ):
            return "owned"
        return "replaced"
    def _release_worktree_directory_handle(self) -> None:
        if self._worktree_dir_fd >= 0:
            os.close(self._worktree_dir_fd)
            self._worktree_dir_fd = -1
    def _quarantine_and_remove_owned_worktree_directory(self, path: str) -> None:
        if self._worktree_dir_fd < 0:
            raise OSError("worktree directory identity is unavailable")
        opened = os.fstat(self._worktree_dir_fd)
        expected = (opened.st_dev, opened.st_ino)
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("worktree directory descriptor no longer names a directory")

        quarantine_path = self._worktree_quarantine_dir
        if quarantine_path is None:
            parent_fd, name = _open_parent_dirfd(path, create_parents=False)
            try:
                try:
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != expected
                ):
                    raise OSError("worktree directory ownership changed before quarantine")
                for _attempt in range(16):
                    quarantine_name = f".opencollab-remove-{uuid.uuid4().hex}"
                    try:
                        os.stat(
                            quarantine_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        break
                else:
                    raise FileExistsError("could not allocate worktree quarantine name")
                os.rename(
                    name,
                    quarantine_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                quarantine_path = os.path.join(os.path.dirname(path), quarantine_name)
                self._worktree_quarantine_dir = quarantine_path
                quarantined = os.stat(
                    quarantine_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(quarantined.st_mode)
                    or (quarantined.st_dev, quarantined.st_ino) != expected
                ):
                    try:
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        os.rename(
                            quarantine_name,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        self._worktree_quarantine_dir = None
                    raise OSError("worktree quarantine identity could not be proved")
            finally:
                os.close(parent_fd)

        assert quarantine_path is not None
        parent_fd, quarantine_name = _open_parent_dirfd(
            quarantine_path,
            create_parents=False,
        )
        try:
            try:
                quarantined = os.stat(
                    quarantine_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise OSError("worktree quarantine disappeared before removal") from exc
            if (
                not stat.S_ISDIR(quarantined.st_mode)
                or (quarantined.st_dev, quarantined.st_ino) != expected
            ):
                raise OSError("worktree quarantine identity changed; refusing removal")
            _sync_clear_pinned_directory(
                self._worktree_dir_fd,
                quarantine_path,
            )
            try:
                current = os.stat(
                    quarantine_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                raise OSError("worktree quarantine disappeared before final removal") from None
            if (current.st_dev, current.st_ino) != expected:
                raise OSError("worktree quarantine identity changed before final removal")
            os.rmdir(quarantine_name, dir_fd=parent_fd)
            self._worktree_quarantine_dir = None
            self._worktree_directory_removed = True
        finally:
            os.close(parent_fd)

    async def _record_owned_branch_oid(self) -> None:
        if not self._branch_cleanup_pending or self._branch_owned_oid is not None:
            return
        ref_name = f"refs/heads/{self._branch}"
        probe = await self._run_git("show-ref", "--hash", "--verify", ref_name)
        if probe.stdout_truncated or probe.stderr_truncated:
            raise OSError("worktree branch ownership probe output was truncated")
        if probe.returncode == 1:
            self._branch_cleanup_pending = False
            return
        if probe.returncode != 0:
            raise OSError("worktree branch ownership probe failed: " + probe.stderr.strip())
        self._branch_owned_oid = _parse_object_id(probe.stdout)

    async def _run_git(
        self,
        *args: str,
        late_compensation: Callable[["_ThreadProcessResult"], None] | None = None,
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
                    f"git {' '.join(args)} timed out after {timeout:g}s; cleanup_quiesced={exc.cleanup_quiesced}"
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
                detail = f"worktree setup compensation failed: {type(cleanup_exc).__name__}: {cleanup_exc}"
                logger.error(detail)
                add_exception_note(original, detail)
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

        base_result = await self._run_git("rev-parse", "--verify", "HEAD^{commit}")
        if base_result.returncode != 0 or base_result.stdout_truncated or base_result.stderr_truncated:
            raise RuntimeError("cannot resolve worktree base commit")
        self._base_commit = base_result.stdout.strip()

        def compensate_claim(result: _ThreadProcessResult) -> None:
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
        self._worktree_dir = os.path.realpath(tempfile.mkdtemp(prefix="opencollab-wt-"))
        self._worktree_directory_removed = False
        self._capture_worktree_directory_handle()
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
        if self._worktree_directory_state(self._worktree_dir) != "owned":
            raise OSError("worktree directory changed while git worktree add completed")
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
                if self._worktree_dir_fd < 0:
                    self._capture_worktree_directory_handle()
                if owned_branch and self._branch_owned_oid is None:
                    try:
                        returncode, stdout, stderr, quiesced = self._sync_run_git(
                            "show-ref",
                            "--hash",
                            "--verify",
                            f"refs/heads/{self._branch}",
                        )
                    except BaseException as exc:
                        errors.append(f"late branch ownership probe failed: {exc}")
                    else:
                        if quiesced and returncode == 0:
                            self._branch_owned_oid = _parse_object_id(stdout)
                        elif quiesced and returncode == 1:
                            self._branch_cleanup_pending = False
                            owned_branch = False
                        else:
                            errors.append(
                                "late branch ownership probe failed: "
                                + stderr.decode(errors="replace").strip()
                            )
                try:
                    returncode, stdout, _stderr, quiesced = self._sync_run_git(
                        "worktree",
                        "list",
                        "--porcelain",
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
                directory_state = self._worktree_directory_state(worktree_dir)
                if directory_state in {"owned", "absent"}:
                    try:
                        returncode, _stdout, _stderr, quiesced = self._sync_run_git(
                            "worktree",
                            "remove",
                            "--force",
                            worktree_dir,
                        )
                        if returncode == 0 and quiesced:
                            self._worktree_registered = False
                            if not os.path.exists(worktree_dir):
                                self._worktree_directory_removed = True
                        else:
                            self._worktree_registered = True
                            errors.append("late git worktree remove failed")
                    except BaseException as exc:
                        logger.error("late git worktree compensation failed: %s", exc)
                        self._worktree_registered = True
                        errors.append(f"late git worktree remove failed: {exc}")
                else:
                    errors.append("late worktree directory ownership changed")
                if self._worktree_directory_state(worktree_dir) == "owned":
                    try:
                        self._quarantine_and_remove_owned_worktree_directory(worktree_dir)
                    except BaseException as exc:
                        errors.append(f"late worktree directory removal failed: {exc}")
                elif os.path.exists(worktree_dir):
                    errors.append("late worktree directory ownership changed")
                try:
                    returncode, _stdout, _stderr, quiesced = self._sync_run_git(
                        "worktree",
                        "prune",
                        "--expire",
                        "now",
                    )
                    if returncode != 0 or not quiesced:
                        errors.append("late git worktree prune failed")
                except BaseException as exc:
                    logger.error("late git worktree prune failed: %s", exc)
                    errors.append(f"late git worktree prune failed: {exc}")
            if (
                owned_branch
                and self._branch_preexisting is False
                and not self._worktree_registered
                and self._worktree_directory_removed
            ):
                try:
                    returncode, _stdout, _stderr, quiesced = self._sync_run_git(
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{self._branch}",
                    )
                except BaseException as exc:
                    logger.error("late git branch probe failed: %s", exc)
                    self._branch_cleanup_pending = True
                    errors.append(f"late git branch probe failed: {exc}")
                else:
                    if quiesced and returncode == 1:
                        self._branch_cleanup_pending = False
                    elif quiesced and returncode == 0 and self._branch_owned_oid is not None:
                        try:
                            (
                                delete_returncode,
                                _stdout,
                                _stderr,
                                delete_quiesced,
                            ) = self._sync_run_git(
                                "update-ref",
                                "-d",
                                f"refs/heads/{self._branch}",
                                self._branch_owned_oid,
                            )
                        except BaseException as exc:
                            errors.append(f"late git branch compare-and-delete failed: {exc}")
                            self._branch_cleanup_pending = True
                        else:
                            self._branch_cleanup_pending = not (delete_quiesced and delete_returncode == 0)
                            if self._branch_cleanup_pending:
                                errors.append("late git branch compare-and-delete failed")
                            else:
                                self._branch_owned_oid = None
                    elif quiesced and returncode == 0:
                        self._branch_cleanup_pending = True
                        errors.append("late branch owned object id is unavailable")
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
        if self._local_env is not None:
            try:
                await self._local_env.cleanup()
            except BaseException as exc:
                errors.append(f"local worktree resource cleanup failed: {exc}")
        if worktree_dir and self._worktree_dir_fd < 0:
            try:
                self._capture_worktree_directory_handle()
            except BaseException as exc:
                errors.append(f"worktree directory ownership capture failed: {exc}")
        ownership_unknown = False
        if worktree_dir and self._worktree_add_attempted:
            try:
                await self._refresh_partial_worktree_ownership()
            except BaseException as exc:
                errors.append(f"worktree ownership probe failed: {exc}")
                ownership_unknown = True
        if self._branch_cleanup_pending and self._branch_owned_oid is None:
            try:
                await self._record_owned_branch_oid()
            except BaseException as exc:
                errors.append(f"worktree branch ownership probe failed: {exc}")

        directory_state = (
            self._worktree_directory_state(worktree_dir)
            if worktree_dir
            else "absent"
        )
        if directory_state in {"replaced", "unverified"}:
            errors.append("worktree directory ownership changed; refusing path cleanup")

        if (
            worktree_dir
            and directory_state in {"owned", "absent"}
            and (self._worktree_registered or ownership_unknown)
        ):
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
                    if not os.path.exists(worktree_dir):
                        self._worktree_directory_removed = True
                else:
                    self._worktree_registered = True
                    errors.append("git worktree remove failed: " + removed.stderr.strip())

        if worktree_dir and self._worktree_directory_state(worktree_dir) == "owned":
            try:
                await _run_owned_blocking_io(
                    self._quarantine_and_remove_owned_worktree_directory,
                    worktree_dir,
                )
            except BaseException as exc:
                errors.append(f"worktree directory removal failed: {exc}")
        elif worktree_dir and os.path.exists(worktree_dir):
            errors.append("worktree directory ownership changed; foreign path preserved")

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

        branch_cleanup_safe = (
            not self._worktree_registered
            and not ownership_unknown
            and self._worktree_directory_removed
        )
        if self._branch_cleanup_pending and branch_cleanup_safe:
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
                elif self._branch_owned_oid is None:
                    errors.append("branch owned object id is unavailable")
                else:
                    try:
                        deleted = await self._run_git(
                            "update-ref",
                            "-d",
                            f"refs/heads/{self._branch}",
                            self._branch_owned_oid,
                        )
                    except BaseException as exc:
                        errors.append(f"git branch compare-and-delete failed: {exc}")
                    else:
                        if deleted.returncode == 0:
                            self._branch_cleanup_pending = False
                            self._branch_owned_oid = None
                        else:
                            errors.append(
                                "git branch compare-and-delete failed: "
                                + deleted.stderr.strip()
                            )

        quarantine_gone = not self._worktree_quarantine_dir or not os.path.exists(
            self._worktree_quarantine_dir
        )
        directory_gone = (not worktree_dir or self._worktree_directory_removed) and quarantine_gone
        if not self._worktree_registered and not ownership_unknown and directory_gone:
            self._release_worktree_directory_handle()
            self._worktree_dir = None
            self._local_env = None
        if (
            not self._branch_cleanup_pending
            and not self._worktree_registered
            and not ownership_unknown
            and directory_gone
        ):
            self._worktree_add_attempted = False
        source_release_safe = (
            not self._add_owner_active
            and not self._branch_cleanup_pending
            and not self._worktree_registered
            and not self._worktree_add_attempted
            and not ownership_unknown
            and directory_gone
        )
        if source_release_safe:
            try:
                self._source_handle.release()
            except BaseException as exc:
                errors.append(f"source repository handle cleanup failed: {exc}")
        if errors and raise_on_error:
            raise OSError("; ".join(errors))
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
