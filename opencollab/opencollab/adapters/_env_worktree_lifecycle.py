"""Ownership and teardown helpers for Git worktree environments."""

from __future__ import annotations

import logging
import os

from opencollab.adapters._env_directory_cleanup import _parse_object_id
from opencollab.adapters._env_file_io import _run_owned_blocking_io
from opencollab.adapters._env_worktree_directory import _OwnedWorktreeDirectoryMixin

logger = logging.getLogger(__name__)


def _registered_worktree_paths(stdout: str | bytes) -> set[str]:
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    return {
        os.path.realpath(line.removeprefix("worktree "))
        for line in text.splitlines()
        if line.startswith("worktree ")
    }


class _WorktreeLifecycleMixin(_OwnedWorktreeDirectoryMixin):
    """Manage descriptor-pinned worktree ownership and retryable teardown."""

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

    def _late_probe_branch_ownership(self, owned_branch: bool, errors: list[str]) -> bool:
        if not owned_branch or self._branch_owned_oid is not None:
            return owned_branch
        try:
            returncode, stdout, stderr, quiesced = self._sync_run_git(
                "show-ref",
                "--hash",
                "--verify",
                f"refs/heads/{self._branch}",
            )
        except BaseException as exc:
            errors.append(f"late branch ownership probe failed: {exc}")
            return owned_branch
        if quiesced and returncode == 0:
            self._branch_owned_oid = _parse_object_id(stdout)
        elif quiesced and returncode == 1:
            self._branch_cleanup_pending = False
            return False
        else:
            errors.append(
                "late branch ownership probe failed: "
                + stderr.decode(errors="replace").strip()
            )
        return owned_branch

    def _late_refresh_registration(
        self,
        worktree_dir: str,
        owned_branch: bool,
        errors: list[str],
    ) -> bool:
        try:
            returncode, stdout, _stderr, quiesced = self._sync_run_git(
                "worktree",
                "list",
                "--porcelain",
            )
        except BaseException as exc:
            logger.error("late git worktree ownership probe failed: %s", exc)
            errors.append(f"late worktree ownership probe failed: {exc}")
            return owned_branch
        registered = (
            returncode == 0
            and quiesced
            and os.path.realpath(worktree_dir) in _registered_worktree_paths(stdout)
        )
        if registered:
            self._worktree_registered = True
            owned_branch = self._branch_preexisting is False
            self._branch_cleanup_pending = owned_branch
        elif returncode != 0 or not quiesced:
            errors.append("late worktree ownership probe was indeterminate")
        return owned_branch

    def _late_remove_registered_worktree(self, worktree_dir: str, errors: list[str]) -> None:
        if self._worktree_directory_state(worktree_dir) not in {"owned", "absent"}:
            errors.append("late worktree directory ownership changed")
            return
        try:
            returncode, _stdout, _stderr, quiesced = self._sync_run_git(
                "worktree",
                "remove",
                "--force",
                worktree_dir,
            )
        except BaseException as exc:
            logger.error("late git worktree compensation failed: %s", exc)
            self._worktree_registered = True
            errors.append(f"late git worktree remove failed: {exc}")
            return
        if returncode == 0 and quiesced:
            self._worktree_registered = False
            if not os.path.exists(worktree_dir):
                self._worktree_directory_removed = True
        else:
            self._worktree_registered = True
            errors.append("late git worktree remove failed")

    def _late_remove_owned_directory(self, worktree_dir: str, errors: list[str]) -> None:
        if self._worktree_directory_state(worktree_dir) == "owned":
            try:
                self._quarantine_and_remove_owned_worktree_directory(worktree_dir)
            except BaseException as exc:
                errors.append(f"late worktree directory removal failed: {exc}")
        elif os.path.exists(worktree_dir):
            errors.append("late worktree directory ownership changed")

    def _late_prune_worktrees(self, errors: list[str]) -> None:
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

    def _late_delete_owned_branch(self, owned_branch: bool, errors: list[str]) -> None:
        if not (
            owned_branch
            and self._branch_preexisting is False
            and not self._worktree_registered
            and self._worktree_directory_removed
        ):
            return
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
            return
        if quiesced and returncode == 1:
            self._branch_cleanup_pending = False
        elif quiesced and returncode == 0 and self._branch_owned_oid is not None:
            self._late_compare_and_delete_branch(errors)
        elif quiesced and returncode == 0:
            self._branch_cleanup_pending = True
            errors.append("late branch owned object id is unavailable")
        else:
            self._branch_cleanup_pending = True
            errors.append("late git branch probe was indeterminate")

    def _late_compare_and_delete_branch(self, errors: list[str]) -> None:
        try:
            returncode, _stdout, _stderr, quiesced = self._sync_run_git(
                "update-ref",
                "-d",
                f"refs/heads/{self._branch}",
                self._branch_owned_oid,
            )
        except BaseException as exc:
            errors.append(f"late git branch compare-and-delete failed: {exc}")
            self._branch_cleanup_pending = True
            return
        self._branch_cleanup_pending = not (quiesced and returncode == 0)
        if self._branch_cleanup_pending:
            errors.append("late git branch compare-and-delete failed")
        else:
            self._branch_owned_oid = None

    def _late_compensate_worktree_add(self) -> None:
        errors: list[str] = []
        try:
            worktree_dir = self._worktree_dir
            owned_branch = self._branch_cleanup_pending
            if worktree_dir:
                if self._worktree_dir_fd < 0:
                    self._capture_worktree_directory_handle()
                owned_branch = self._late_probe_branch_ownership(owned_branch, errors)
                owned_branch = self._late_refresh_registration(
                    worktree_dir,
                    owned_branch,
                    errors,
                )
                self._late_remove_registered_worktree(worktree_dir, errors)
                self._late_remove_owned_directory(worktree_dir, errors)
                self._late_prune_worktrees(errors)
            self._late_delete_owned_branch(owned_branch, errors)
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
        self._worktree_registered = expected in _registered_worktree_paths(listed.stdout)
        if self._worktree_registered and self._branch_preexisting is False:
            self._branch_cleanup_pending = True

    async def _cleanup_local_environment(self, errors: list[str]) -> None:
        if self._local_env is None:
            return
        try:
            await self._local_env.cleanup()
        except BaseException as exc:
            errors.append(f"local worktree resource cleanup failed: {exc}")

    async def _probe_cleanup_ownership(self, worktree_dir: str | None, errors: list[str]) -> bool:
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
        return ownership_unknown

    async def _remove_registered_worktree(
        self,
        worktree_dir: str | None,
        ownership_unknown: bool,
        errors: list[str],
    ) -> bool:
        if not worktree_dir:
            return ownership_unknown
        directory_state = self._worktree_directory_state(worktree_dir)
        if directory_state in {"replaced", "unverified"}:
            errors.append("worktree directory ownership changed; refusing path cleanup")
        if not (
            directory_state in {"owned", "absent"}
            and (self._worktree_registered or ownership_unknown)
        ):
            return ownership_unknown
        try:
            removed = await self._run_git("worktree", "remove", "--force", worktree_dir)
        except BaseException as exc:
            errors.append(f"git worktree remove failed: {exc}")
            return ownership_unknown
        if removed.returncode == 0:
            self._worktree_registered = False
            if not os.path.exists(worktree_dir):
                self._worktree_directory_removed = True
            return False
        self._worktree_registered = True
        errors.append("git worktree remove failed: " + removed.stderr.strip())
        return ownership_unknown

    async def _remove_owned_worktree_directory(
        self,
        worktree_dir: str | None,
        errors: list[str],
    ) -> None:
        if not worktree_dir:
            return
        if self._worktree_directory_state(worktree_dir) == "owned":
            try:
                await _run_owned_blocking_io(
                    self._quarantine_and_remove_owned_worktree_directory,
                    worktree_dir,
                )
            except BaseException as exc:
                errors.append(f"worktree directory removal failed: {exc}")
        elif os.path.exists(worktree_dir):
            errors.append("worktree directory ownership changed; foreign path preserved")

    async def _prune_worktree_registration(
        self,
        worktree_dir: str | None,
        ownership_unknown: bool,
        errors: list[str],
    ) -> bool:
        if not worktree_dir or not (self._worktree_registered or ownership_unknown):
            return ownership_unknown
        try:
            pruned = await self._run_git("worktree", "prune", "--expire", "now")
            if pruned.returncode != 0:
                errors.append("git worktree prune failed: " + pruned.stderr.strip())
                return ownership_unknown
            listed = await self._run_git("worktree", "list", "--porcelain")
            if listed.stdout_truncated:
                raise RuntimeError("git worktree list output was truncated")
            if (
                listed.returncode == 0
                and os.path.realpath(worktree_dir)
                not in _registered_worktree_paths(listed.stdout)
            ):
                self._worktree_registered = False
                return False
        except BaseException as exc:
            errors.append(f"git worktree prune failed: {exc}")
        return ownership_unknown

    async def _cleanup_owned_branch(
        self,
        ownership_unknown: bool,
        errors: list[str],
    ) -> None:
        branch_cleanup_safe = (
            not self._worktree_registered
            and not ownership_unknown
            and self._worktree_directory_removed
        )
        if not self._branch_cleanup_pending or not branch_cleanup_safe:
            return
        try:
            branch_probe = await self._run_git(
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{self._branch}",
            )
        except BaseException as exc:
            errors.append(f"branch cleanup probe failed: {exc}")
            return
        if branch_probe.returncode == 1:
            self._branch_cleanup_pending = False
        elif branch_probe.returncode != 0:
            errors.append("branch cleanup probe failed: " + branch_probe.stderr.strip())
        elif self._branch_owned_oid is None:
            errors.append("branch owned object id is unavailable")
        else:
            await self._compare_and_delete_owned_branch(errors)

    async def _compare_and_delete_owned_branch(self, errors: list[str]) -> None:
        try:
            deleted = await self._run_git(
                "update-ref",
                "-d",
                f"refs/heads/{self._branch}",
                self._branch_owned_oid,
            )
        except BaseException as exc:
            errors.append(f"git branch compare-and-delete failed: {exc}")
            return
        if deleted.returncode == 0:
            self._branch_cleanup_pending = False
            self._branch_owned_oid = None
        else:
            errors.append("git branch compare-and-delete failed: " + deleted.stderr.strip())

    def _cleanup_directory_gone(self, worktree_dir: str | None) -> bool:
        quarantine_gone = not self._worktree_quarantine_dir or not os.path.exists(
            self._worktree_quarantine_dir
        )
        return (not worktree_dir or self._worktree_directory_removed) and quarantine_gone

    def _finalize_cleanup_state(
        self,
        worktree_dir: str | None,
        ownership_unknown: bool,
        errors: list[str],
    ) -> None:
        directory_gone = self._cleanup_directory_gone(worktree_dir)
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

    async def _cleanup_worktree_resources(self, *, raise_on_error: bool) -> None:
        errors: list[str] = []
        if self._add_owner_active:
            error = "git worktree add owner is still performing compensation"
            if raise_on_error:
                raise OSError(error)
            logger.error(error)
            return
        worktree_dir = self._worktree_dir
        await self._cleanup_local_environment(errors)
        ownership_unknown = await self._probe_cleanup_ownership(worktree_dir, errors)
        ownership_unknown = await self._remove_registered_worktree(
            worktree_dir,
            ownership_unknown,
            errors,
        )
        await self._remove_owned_worktree_directory(worktree_dir, errors)
        ownership_unknown = await self._prune_worktree_registration(
            worktree_dir,
            ownership_unknown,
            errors,
        )
        await self._cleanup_owned_branch(ownership_unknown, errors)
        self._finalize_cleanup_state(worktree_dir, ownership_unknown, errors)
        if errors and raise_on_error:
            raise OSError("; ".join(errors))
