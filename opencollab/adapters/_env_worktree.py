"""Git worktree environment with a non-Git copy fallback."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import tempfile
import uuid

from opencollab.adapters._env_base import Environment, ExecResult, TextFileRange
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import run_process
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note

WORKTREE_GIT_TIMEOUT_SECONDS = 30.0
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
_ZERO_OID = "0" * 40
# One HEAD reflog entry as ``git log -g --format=%H%x09%gs`` writes it: the
# commit HEAD was moved to, and the message saying how it got there.
_REFLOG_ENTRY_RE = re.compile(r"^([0-9a-f]{40})\t(.*)$")
# Reflog messages for a commit this worktree made itself: ``commit: <subject>``,
# and the parenthesised variants ``commit (initial)``, ``commit (amend)``,
# ``commit (merge)``. Every other way HEAD moves adopts a commit from elsewhere.
_OWN_COMMIT_REFLOG_PREFIX = "commit"
_PORCELAIN_STATUS_CHARS = frozenset(" MADRCU?!")


def _dirty_path_preview(output: str, *, limit: int = 12) -> str:
    paths: list[str] = []
    for record in filter(None, output.split("\0")):
        if (
            len(record) >= 4
            and record[0] in _PORCELAIN_STATUS_CHARS
            and record[1] in _PORCELAIN_STATUS_CHARS
            and record[2] == " "
        ):
            paths.append(record[3:])
        else:
            paths.append(record)
    preview = ", ".join(repr(path) for path in paths[:limit])
    if len(paths) > limit:
        preview += f", ... ({len(paths) - limit} more)"
    return preview


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
        self._source_subdir = ""
        self._repository_root: str | None = None
        self._git_diff_delivery_pending = False
        # The revision the last ``get_diff`` measured against. Read by the
        # scheduler's ``worktree_changes`` record so a row states its own
        # baseline; ``None`` until a diff has been taken, and on the non-Git
        # copy path, which has no revision to name.
        self._diff_base: str | None = None

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
        if self._git_mode:
            top_level = await self._git("rev-parse", "--show-toplevel")
            if (
                top_level.returncode != 0
                or top_level.stdout_truncated
                or top_level.stderr_truncated
            ):
                raise RuntimeError("cannot resolve Git repository root")
            repository_root = os.path.realpath(top_level.stdout.strip())
            self._repository_root = repository_root
            try:
                contained = os.path.commonpath((repository_root, self._source))
            except ValueError as exc:
                raise RuntimeError("source workspace is outside its Git repository") from exc
            if contained != repository_root:
                raise RuntimeError("source workspace is outside its Git repository")
            relative_source = os.path.relpath(self._source, repository_root)
            self._source_subdir = "" if relative_source == "." else relative_source
        try:
            if self._git_mode:
                await self._setup_git_worktree()
            else:
                await self._setup_directory_copy()
            assert self._worktree_dir is not None
            exposed_workspace = os.path.join(
                self._worktree_dir,
                self._source_subdir,
            )
            if not os.path.isdir(exposed_workspace):
                raise RuntimeError("source subdirectory is absent from the worktree")
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
        self.workspace = exposed_workspace
        self.host_workspace = exposed_workspace
        self._local_env = LocalEnvironment(exposed_workspace)
        return exposed_workspace

    async def _setup_git_worktree(self) -> None:
        status = await self._git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if (
            status.returncode != 0
            or status.stdout_truncated
            or status.stderr_truncated
        ):
            raise RuntimeError("cannot verify that the source workspace is clean")
        if status.stdout:
            raise RuntimeError(
                "source workspace has uncommitted changes: "
                f"{_dirty_path_preview(status.stdout)}"
            )
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
        await self._initialize_available_submodules()

    async def _git_in(self, workspace: str, *args: str) -> ExecResult:
        result = await run_process(
            ("git", "-C", workspace, *args),
            shell=False,
            timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
        )
        return result.to_exec_result()

    async def _configured_source_submodules(
        self,
        source_repository: str,
        *,
        source_prefix: str,
    ) -> list[tuple[str, str, str]]:
        modules_file = os.path.join(source_repository, ".gitmodules")
        if not os.path.isfile(modules_file):
            return []
        configured = await self._git_in(
            source_repository,
            "config",
            "--file",
            modules_file,
            "--get-regexp",
            r"^submodule\..*\.path$",
        )
        if configured.returncode == 1:
            return []
        if (
            configured.returncode != 0
            or configured.stdout_truncated
            or configured.stderr_truncated
        ):
            raise RuntimeError("cannot inspect Git submodule configuration")

        submodules: list[tuple[str, str, str]] = []
        for line in configured.stdout.splitlines():
            try:
                key, configured_path = line.split(maxsplit=1)
            except ValueError as exc:
                raise RuntimeError("invalid Git submodule path configuration") from exc
            prefix = "submodule."
            suffix = ".path"
            if not key.startswith(prefix) or not key.endswith(suffix):
                raise RuntimeError("invalid Git submodule path configuration")
            name = key[len(prefix) : -len(suffix)]
            normalized_path = configured_path.replace("\\", "/").strip("/")
            if (
                not normalized_path
                or normalized_path == ".."
                or normalized_path.startswith("../")
            ):
                raise RuntimeError("invalid Git submodule path configuration")
            if source_prefix and not (
                normalized_path == source_prefix
                or normalized_path.startswith(f"{source_prefix}/")
            ):
                continue
            source_module = os.path.realpath(
                os.path.join(source_repository, *normalized_path.split("/"))
            )
            try:
                contained = os.path.commonpath((source_repository, source_module))
            except ValueError:
                contained = ""
            if contained != source_repository or not os.path.isdir(source_module):
                raise RuntimeError(
                    f"Git submodule is not initialized in source workspace: {configured_path}"
                )
            available = await self._git_in(
                source_module,
                "rev-parse",
                "--show-toplevel",
            )
            if (
                available.returncode != 0
                or available.stdout_truncated
                or available.stderr_truncated
                or os.path.realpath(available.stdout.strip()) != source_module
            ):
                raise RuntimeError(
                    f"Git submodule is not initialized in source workspace: {configured_path}"
                )
            submodules.append((name, normalized_path, source_module))
        return submodules

    async def _initialize_source_submodule_tree(
        self,
        source_repository: str,
        target_repository: str,
        *,
        source_prefix: str,
        active_sources: set[str],
    ) -> None:
        source_repository = os.path.realpath(source_repository)
        if source_repository in active_sources:
            raise RuntimeError("cyclic initialized Git submodule source")
        active_sources.add(source_repository)
        try:
            submodules = await self._configured_source_submodules(
                source_repository,
                source_prefix=source_prefix,
            )
            for name, normalized_path, source_module in submodules:
                updated = await self._git_in(
                    target_repository,
                    "-c",
                    "protocol.allow=never",
                    "-c",
                    "protocol.file.allow=always",
                    "-c",
                    f"submodule.{name}.url={source_module}",
                    "submodule",
                    "update",
                    "--init",
                    "--no-fetch",
                    "--",
                    normalized_path,
                )
                if (
                    updated.returncode != 0
                    or updated.stdout_truncated
                    or updated.stderr_truncated
                ):
                    detail = updated.stderr.strip() or "submodule checkout failed"
                    raise RuntimeError(
                        "cannot initialize source-available submodule "
                        f"{normalized_path}: {detail}"
                    )
                target_module = os.path.realpath(
                    os.path.join(
                        target_repository,
                        *normalized_path.split("/"),
                    )
                )
                await self._initialize_source_submodule_tree(
                    source_module,
                    target_module,
                    source_prefix="",
                    active_sources=active_sources,
                )
        finally:
            active_sources.remove(source_repository)

    async def _initialize_available_submodules(self) -> None:
        repository_root = self._repository_root
        worktree_dir = self._worktree_dir
        if repository_root is None or worktree_dir is None:
            return
        await self._initialize_source_submodule_tree(
            repository_root,
            worktree_dir,
            source_prefix=self._source_subdir.replace(os.sep, "/").rstrip("/"),
            active_sources=set(),
        )

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

    @property
    def diff_base(self) -> str | None:
        """The revision the last ``get_diff`` measured against, if any."""
        return self._diff_base

    async def _resolve_diff_base(self) -> str:
        """The commit this worktree's current stretch of work started from.

        ``_base_commit`` — the HEAD pinned when the worktree was created — is
        the right base only for as long as the worktree stays on it, and it
        stops being right the moment one agent adopts another's work. Under the
        handoff protocol a coder commits inside its own worktree and sends the
        sha to a tester, who runs ``git checkout <sha>`` in a linked worktree of
        the same repository. Measured against the creation base, the tester's
        diff then contains every file the coder touched, and the scheduler's
        ``worktree_changes`` record files all of them under the tester — which
        is the per-agent attribution the record exists to provide.

        So the base is the commit HEAD was last *moved onto* rather than the one
        it *grew from*: the newest HEAD reflog entry that is not a commit this
        worktree made. A checkout, a reset, or a merge that brings in someone
        else's history moves the base forward onto what was adopted; the agent's
        own commits leave it where it was, so work the agent committed itself
        still reads as its own. When the newest such entry is the one git wrote
        when the worktree was created, the answer is exactly ``_base_commit``,
        so an agent that never took a handoff diffs precisely as it did before.

        Nothing has to be told when a stretch of work begins: git already
        records every HEAD move per worktree, in ``logs/HEAD`` under
        ``.git/worktrees/<id>/``. That also makes the two callers of
        ``get_diff`` — the parent's copy of the diff and the trace record — agree
        by construction, since both resolve the base here rather than holding
        one of their own.

        Falls back to ``_base_commit`` when the reflog cannot be read or parsed.
        A repository with ``core.logAllRefUpdates`` off keeps no such record, and
        the creation base is the honest answer where there is nothing to read.
        """
        assert self._base_commit is not None
        worktree = self._worktree_dir
        if worktree is None:
            return self._base_commit
        reflog = await self._git_in(worktree, "log", "-g", "--format=%H%x09%gs", "HEAD")
        if reflog.returncode != 0 or reflog.stdout_truncated or reflog.stderr_truncated:
            return self._base_commit
        for line in reflog.stdout.splitlines():
            entry = _REFLOG_ENTRY_RE.match(line)
            if entry is None:
                continue
            commit, message = entry.group(1), entry.group(2)
            if message.startswith(_OWN_COMMIT_REFLOG_PREFIX):
                continue
            return commit
        return self._base_commit

    async def get_diff(self) -> str:
        self._ensure_active()
        if self._local_env is None:
            return ""
        if not self._git_mode:
            self._diff_base = None
            diff = await self._directory_copy_diff()
            self._copy_exported_diff = diff
            return diff
        if self._base_commit is None:
            raise RuntimeError("worktree base commit is unavailable")
        base_revision = await self._resolve_diff_base()
        self._diff_base = base_revision
        self._git_diff_delivery_pending = True
        result = await self._local_env.exec_cmd(
            guarded_staged_diff_command(base_revision=base_revision)
        )
        if result.stdout_truncated or result.stderr_truncated:
            raise RuntimeError(
                f"worktree diff exceeded capture limit; worktree retained at {self.workspace}"
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"git exited with status {result.returncode}"
            raise RuntimeError(
                f"worktree diff extraction failed: {detail}; "
                f"worktree retained at {self.workspace}"
            )
        self._git_diff_delivery_pending = False
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

    async def read_text_range(
        self,
        path: str,
        *,
        offset: int,
        limit: int,
        max_chars: int,
    ) -> TextFileRange:
        self._ensure_active()
        if self._local_env is None:
            await self.setup()
        assert self._local_env is not None
        return await self._local_env.read_text_range(
            path,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )

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
                raise RuntimeError("worktree cleanup failed") from exc
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
            if self._git_mode and self._git_diff_delivery_pending:
                raise RuntimeError(
                    "refusing to clean undelivered Git worktree changes; "
                    f"worktree retained at {self.workspace}"
                )
            self.revoke()
            await await_owned_operation(
                self._cleanup_resources(),
                propagate_cancellation=True,
            )

    async def abort(self) -> None:
        async with self._lifecycle_lock:
            await self._ensure_directory_copy_changes_exported()
            if self._git_mode and self._git_diff_delivery_pending:
                raise RuntimeError(
                    "refusing to clean undelivered Git worktree changes; "
                    f"worktree retained at {self.workspace}"
                )
            self.revoke()
            await await_owned_operation(
                self._cleanup_resources(),
                propagate_cancellation=True,
            )

__all__ = ["WorktreeEnvironment"]
