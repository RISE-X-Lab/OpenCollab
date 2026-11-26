"""Environment-backed isolated candidate worktrees."""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, ClassVar

from opencollab.adapters._candidate_adoption import _mounted_recovery_paths
from opencollab.adapters._candidate_increment import _apply_increment as _apply_increment
from opencollab.adapters._candidate_increment import _safe_path
from opencollab.adapters._candidate_physical import (
    _apply_physical,
    _capture_physical,
    _has_worktree_conversions,
    _initialized_submodule_paths,
    _physical_git,
    _physical_paths,
    _physical_submodule_trees,
    _physical_tree,
    _submodule_base_tree,
    _tree_patch,
)
from opencollab.adapters._candidate_source import _source_snapshot
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import ProcessCleanupError
from opencollab.adapters._env_vm import VMEnvironment
from opencollab.adapters._env_worktree_submodules import _initialize_source_available_submodules
from opencollab.adapters._git_patch_bytes import _complete, _complete_patch
from opencollab.adapters._git_patch_bytes import _patch_temporary as _candidate_temporary
from opencollab.adapters.env import DockerWorkspaceEnvironment
from opencollab.adapters.git_patch import (
    _git_command_and_config_guard,
    _submodule_patch_guard,
    _submodule_status_command,
    guarded_staged_diff_command,
)
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note

CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS = 900.0

def _repository_exclusions(prefix: str, paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(posixpath.normpath(posixpath.join(prefix, _safe_path(path))) for path in paths)

async def _raw_diff(environment: Any, exclude_paths: Sequence[str] = ()) -> str:
    workspace = getattr(environment, "workspace", None)
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("candidate diff workspace is unavailable")
    return await _raw_diff_at(environment, workspace, exclude_paths)

async def _raw_diff_at(
    environment: Any,
    workspace: str,
    exclude_paths: Sequence[str] = (),
    *,
    base_revision: str = "HEAD",
    index_file: str | None = None,
    untracked_exclude_paths: Sequence[str] = (),
) -> str:
    excluded = tuple(_safe_path(path) for path in exclude_paths)
    pathspec = ""
    if excluded:
        pathspec = " -- . " + " ".join(
            shlex.quote(f":(exclude,literal){path}") for path in excluded
        )
    git = f"git --no-optional-locks -c core.quotePath=true -C {shlex.quote(workspace)}"
    cached = ""
    if index_file is not None:
        git = f"GIT_INDEX_FILE={shlex.quote(index_file)} {git}"
        cached = " --cached"
    tracked = await _complete_patch(
        await environment.exec_cmd(
            f"{git} -c core.filemode=true --no-pager diff{cached} {shlex.quote(base_revision)} --binary --full-index "
            "--no-ext-diff --no-textconv --no-color --unified=3 --src-prefix=a/ --dst-prefix=b/"
            + pathspec,
            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
        ),
        "candidate tracked diff", environment, workspace, base_revision, index_file,
    )
    untracked_paths = (*excluded, *(_safe_path(path) + "/" for path in untracked_exclude_paths))
    untracked_pathspec = " -- . " + shlex.join(
        f":(exclude,literal){path}" for path in untracked_paths
    ) if untracked_paths else ""
    untracked = _complete(
        await environment.exec_cmd(
            f"{git} ls-files --others --exclude-standard -z"
            + untracked_pathspec,
            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
        ),
        "candidate untracked listing",
    )
    parts = [tracked] if tracked.strip() else []
    for path in (item for item in untracked.split("\0") if item):
        patch = await _complete_patch(
            await environment.exec_cmd(
                "git -c core.quotePath=true -C "
                f"{shlex.quote(workspace)} --no-pager diff --no-index --binary --full-index "
                "--no-ext-diff --no-textconv --no-color --unified=3 --src-prefix=a/ --dst-prefix=b/ "
                f"-- /dev/null {shlex.quote(path)}",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            f"candidate untracked diff for {path}", environment, workspace, base_revision, None,
            allowed=(0, 1),
        )
        if patch.strip():
            parts.append(patch)
    return "".join(parts)

async def _assert_patchable_submodules(
    environment: Any,
    workspace: str,
    base_revision: str,
    *,
    index_file: str | None = None,
    reject_ignored_untracked: bool = True,
    exclude_untracked_paths: Sequence[str] = (),
    exclude_paths: Sequence[str] = (),
) -> None:
    git = f"git --no-optional-locks -C {shlex.quote(workspace)}"
    indexed_git = f"GIT_INDEX_FILE={shlex.quote(index_file)} {git}" if index_file is not None else None
    _complete(
        await environment.exec_cmd(
            _submodule_patch_guard(
                git, base_revision, indexed_git_command=indexed_git,
                reject_ignored_untracked=reject_ignored_untracked,
                exclude_untracked_paths=exclude_untracked_paths,
                exclude_paths=exclude_paths,
            ),
            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
        ),
        "submodule patch inspection",
    )

@dataclass(slots=True)
class _CandidateLease:
    shares_source_execution: ClassVar[bool] = True
    base_environment: Any
    environment: Any | None
    source_workspace: str
    candidate_workspace: str
    base_revision: str
    workspace_prefix: str = ""
    cleaned: bool = False
    physical_base_revision: str | None = None
    delivery_diff: str | None = None
    delivery_paths: tuple[str, ...] = ()
    strict_capture: bool = False
    source_untracked_exclusions: tuple[str, ...] = ()
    physical_source_submodules: tuple[tuple[str, str], ...] = ()
    physical_candidate_submodules: tuple[tuple[str, str], ...] = ()
    initialized_source_submodules: tuple[str, ...] = ()
    initialized_candidate_submodules: tuple[str, ...] = ()

    async def _submodule_contents_changed(
        self, workspace: str, original: tuple[tuple[str, str], ...], *,
        initialized: Sequence[str],
        excluded: Sequence[str] = (), untracked_exclusions: Sequence[str] = (),
    ) -> bool:
        if "." in excluded:
            return False
        current_paths = await _initialized_submodule_paths(
            self.base_environment, workspace, strict=self.strict_capture,
            source_prefix=self.workspace_prefix.rstrip("/"),
        )
        if any(
            not any(path == item or path.startswith(item + "/") for item in excluded)
            for path in set(initialized) ^ set(current_paths)
        ):
            return True
        current_modules = dict(await _physical_submodule_trees(
            self.base_environment, workspace, strict=self.strict_capture,
            source_prefix=self.workspace_prefix.rstrip("/"),
            include_paths=tuple(path for path, _tree in original),
            exclude_paths=untracked_exclusions,
            module_paths=current_paths,
        ))
        original_modules = dict(original)
        for path in original_modules.keys() | current_modules.keys():
            if any(path == item or path.startswith(item + "/") for item in excluded):
                continue
            before, after = original_modules.get(path), current_modules.get(path)
            if before is None and after is not None and path in initialized:
                before = await _submodule_base_tree(
                    self.base_environment, workspace, self.base_revision, path, strict=self.strict_capture,
                )
            if before == after:
                continue
            if before is None or after is None or not excluded:
                return True
            if (await _tree_patch(
                self.base_environment, posixpath.join(workspace, path), before, after,
                exclude_paths=tuple(item[len(path) + 1:] for item in excluded if item.startswith(path + "/")),
                strict=self.strict_capture,
            )).strip():
                return True
        return False

    async def source_contents_changed(self) -> bool:
        return await self.physical_source_changed(force=True)

    async def physical_source_changed(self, exclude_paths: Sequence[str] = (), *, force: bool = False) -> bool:
        excluded = _repository_exclusions(self.workspace_prefix, exclude_paths)
        untracked_exclusions = (
            *self.source_untracked_exclusions,
            *await _mounted_recovery_paths(self.base_environment, self.source_workspace),
        )
        if await self._submodule_contents_changed(
            self.source_workspace, self.physical_source_submodules,
            initialized=self.initialized_source_submodules,
            excluded=excluded, untracked_exclusions=untracked_exclusions,
        ):
            return True
        original_tree = self.physical_base_revision
        if original_tree is None:
            if not force and not await _has_worktree_conversions(
                self.base_environment, self.source_workspace, strict=self.strict_capture,
                exclude_paths=excluded, exclude_untracked_paths=untracked_exclusions,
            ):
                return False
            # Without initial conversions, the ordinary source tree already
            # records the source's physical contents.
            original_tree = _complete(await self.base_environment.exec_cmd(
                f"git -C {shlex.quote(self.source_workspace)} rev-parse --verify "
                f"{shlex.quote(self.base_revision + '^{tree}')}",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ), "candidate original source tree").strip()
        current = await _physical_tree(
            self.base_environment, self.source_workspace, self.base_revision,
            exclude_paths=untracked_exclusions, strict=self.strict_capture,
        )
        if not exclude_paths:
            return current != original_tree
        return bool((await _tree_patch(
            self.base_environment, self.source_workspace, original_tree, current,
            exclude_paths=excluded,
            strict=self.strict_capture,
        )).strip())

    async def _assert_physical_submodules(self, exclude_paths: Sequence[str] = ()) -> None:
        if await self._submodule_contents_changed(
            self.candidate_workspace, self.physical_candidate_submodules,
            initialized=self.initialized_candidate_submodules,
            excluded=exclude_paths,
        ):
            raise RuntimeError("submodule physical changes cannot be delivered as a repository patch")

    async def diff(self, exclude_paths: Sequence[str] = ()) -> str:
        excluded = _repository_exclusions(self.workspace_prefix, exclude_paths)
        await self._assert_physical_submodules(excluded)
        async with _candidate_temporary(
            self.base_environment, self.source_workspace,
            "", prefix=".candidate-capture-index-",
        ) as index_file:
            git = (
                f"GIT_INDEX_FILE={shlex.quote(index_file)} "
                f"git -c core.filemode=true -C {shlex.quote(self.candidate_workspace)}"
            )
            # Stage the current contents in an owned temporary index. Candidate
            # commits and index resets leave the same delivered file changes.
            _complete(
                await self.base_environment.exec_cmd(
                    f"{git} read-tree HEAD && {git} add --all -- .",
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "candidate contents capture",
            )
            original_git = f"git -C {shlex.quote(self.candidate_workspace)}"
            known = _complete(
                await self.base_environment.exec_cmd(
                    f"{original_git} ls-tree -r --name-only -z {shlex.quote(self.base_revision)} "
                    f"&& {original_git} ls-files --cached -z",
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "candidate known paths",
            )
            remaining = _complete(
                await self.base_environment.exec_cmd(
                    f"{git} ls-files --others -z",
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "candidate remaining files",
            )
            # Retain existing source and staged files when new ignore rules hide
            # them. The remaining listing supplies current files, so deletions
            # and file/directory replacements follow their actual contents.
            omitted = sorted(set(known.split("\0")).intersection(remaining.split("\0")) - {""})
            if omitted:
                async with _candidate_temporary(
                    self.base_environment, self.source_workspace,
                    "".join(f":(literal){path}\0" for path in omitted),
                    prefix=".candidate-capture-paths-",
                ) as paths_file:
                    _complete(
                        await self.base_environment.exec_cmd(
                            f"{git} add --force --pathspec-from-file={shlex.quote(paths_file)} --pathspec-file-nul",
                            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                        ),
                        "candidate known files capture",
                    )
            # A repository patch carries gitlink identities, not the module's
            # files. Ordinary Git apply also leaves module checkouts unchanged.
            # Let the caller retain this lease instead of delivering a partial
            # result and deleting the only workspace with the module changes.
            await _assert_patchable_submodules(
                self.base_environment, self.candidate_workspace,
                self.base_revision, index_file=index_file, exclude_paths=excluded,
            )
            return await _raw_diff_at(
                self.base_environment,
                self.candidate_workspace,
                excluded,
                base_revision=self.base_revision,
                index_file=index_file,
            )

    async def capture_changes(self, exclude_paths: Sequence[str] = (), *, force_physical: bool = False) -> str:
        if self.physical_base_revision is None and not force_physical:
            converted = await _has_worktree_conversions(
                self.base_environment, self.candidate_workspace, strict=self.strict_capture,
            )
            if not converted:
                patch = await self.diff(exclude_paths)
                self.delivery_diff, self.delivery_paths = patch, ()
                return patch
        await self._assert_physical_submodules()
        if not self.strict_capture:
            await _assert_patchable_submodules(
                self.base_environment, self.candidate_workspace, self.base_revision,
            )
        captured = await _capture_physical(
            self.base_environment, self.candidate_workspace, self.base_revision, strict=self.strict_capture,
        )
        excluded = _repository_exclusions(self.workspace_prefix, exclude_paths)
        self.delivery_diff = await _tree_patch(
            self.base_environment, self.candidate_workspace,
            self.physical_base_revision or self.base_revision, captured.tree,
            exclude_paths=excluded,
            strict=self.strict_capture,
        )
        self.delivery_paths = tuple(dict.fromkeys((
            *await _physical_paths(
                self.base_environment, self.source_workspace, self.delivery_diff, strict=self.strict_capture,
            ),
            *await _physical_paths(
                self.base_environment, self.candidate_workspace, self.delivery_diff, strict=self.strict_capture,
            ),
        )))
        return await _tree_patch(
            self.base_environment, self.candidate_workspace,
            self.base_revision, captured.normalized_tree, exclude_paths=excluded,
            strict=self.strict_capture,
        )

    async def close_execution(self) -> None:
        """Close native resources while retaining the candidate worktree."""
        if self.environment is not None:
            await await_owned_operation(self.environment.cleanup(), propagate_cancellation=True)

    async def cleanup(self) -> None:
        await await_owned_operation(self._cleanup_resources(), propagate_cancellation=True)

    async def _cleanup_resources(self) -> None:
        if self.cleaned:
            return
        await self.close_execution()
        git = f"git -C {shlex.quote(self.source_workspace)}"
        listed = _complete(
            await self.base_environment.exec_cmd(f"{git} worktree list --porcelain -z", timeout=120),
            "candidate worktree cleanup listing",
        )
        normalize = os.path.realpath if isinstance(self.base_environment, LocalEnvironment) else posixpath.normpath
        registered = {
            normalize(field.removeprefix("worktree "))
            for field in listed.split("\0")
            if field.startswith("worktree ")
        }
        if normalize(self.candidate_workspace) in registered:
            command = f"{git} worktree remove --force -- {shlex.quote(self.candidate_workspace)}"
        else:
            command = f"rm -rf -- {shlex.quote(self.candidate_workspace)}"
        _complete(
            await self.base_environment.exec_cmd(command, timeout=120),
            "candidate worktree cleanup",
        )
        self.cleaned = True

class EnvCandidateWorkspace:
    """Candidate workspace port implemented over one task environment."""

    def __init__(
        self, environment: Any, *, workspace: str | None = None,
        owned_save_dir: str | None = None,
    ) -> None:
        self._environment = environment
        self._workspace = workspace or getattr(environment, "workspace", None)
        if not isinstance(self._workspace, str) or not self._workspace:
            raise ValueError("candidate source workspace is unavailable")
        self._owned_save_dir: str | None = None
        if owned_save_dir is not None and isinstance(environment, LocalEnvironment):
            source = os.path.realpath(self._workspace)
            save_dir = os.path.realpath(owned_save_dir)
            try:
                covers_source = os.path.commonpath((source, save_dir)) == save_dir
            except ValueError:
                covers_source = False
            if not covers_source:
                self._owned_save_dir = save_dir

    def _owned_untracked_paths(self, repository_root: str) -> tuple[str, ...]:
        if self._owned_save_dir is None:
            return ()
        repository_root = os.path.realpath(repository_root)
        try:
            in_repository = os.path.commonpath((
                self._owned_save_dir, repository_root,
            )) == repository_root
        except ValueError:
            in_repository = False
        if not in_repository:
            return ()
        return (os.path.relpath(self._owned_save_dir, repository_root).replace(os.sep, "/"),)

    async def _source_untracked_paths(self, repository_root: str) -> tuple[str, ...]:
        return (
            *self._owned_untracked_paths(repository_root),
            *await _mounted_recovery_paths(self._environment, repository_root),
        )

    async def _repository_root(self) -> str:
        return _complete(
            await self._environment.exec_cmd(
                f"git -C {shlex.quote(self._workspace)} rev-parse --show-toplevel",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            "candidate repository root",
        ).removesuffix("\n")

    async def _workspace_prefix(self) -> str:
        return _complete(
            await self._environment.exec_cmd(
                f"git -C {shlex.quote(self._workspace)} rev-parse --show-prefix",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            "candidate workspace location",
        ).removesuffix("\n")

    async def _source_patch(
        self, repository_root: str, base_revision: str,
        exclude_paths: Sequence[str] = (),
        *, untracked_exclusions: Sequence[str] = (),
    ) -> str:
        # Keep staged additions in a temporary index without its skip hints,
        # then stage the current tracked bytes without changing the source index.
        source_index = _complete(
            await self._environment.exec_cmd(
                "mktemp", timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            "candidate source temporary index",
        ).removesuffix("\n")
        source_paths = source_index + ".tracked"
        try:
            repository_git = f"git --no-optional-locks -C {shlex.quote(repository_root)}"
            git = (
                f"GIT_INDEX_FILE={shlex.quote(source_index)} "
                f"git --no-optional-locks -c core.filemode=true -C {shlex.quote(repository_root)}"
            )
            _complete(
                await self._environment.exec_cmd(
                    f"{git} read-tree {shlex.quote(base_revision)} && "
                    f"{repository_git} ls-files --stage -z > {shlex.quote(source_paths)} && "
                    f"{git} update-index -z --index-info < {shlex.quote(source_paths)} && "
                    f"{git} add -u",
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "candidate source contents staging",
            )
            await _assert_patchable_submodules(
                self._environment, repository_root, base_revision,
                index_file=source_index, reject_ignored_untracked=False,
                exclude_untracked_paths=untracked_exclusions,
                exclude_paths=exclude_paths,
            )
            return await _raw_diff_at(
                self._environment, repository_root, exclude_paths,
                base_revision=base_revision, index_file=source_index,
                untracked_exclude_paths=untracked_exclusions,
            )
        finally:
            async def remove_index() -> None:
                _complete(
                    await self._environment.exec_cmd(
                        f"rm -f -- {shlex.quote(source_index)} {shlex.quote(source_paths)}",
                        timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                    ),
                    "candidate source temporary index cleanup",
                )

            await await_owned_operation(remove_index(), propagate_cancellation=True)

    async def _candidate_environment(self, path: str) -> Any:
        if isinstance(self._environment, LocalEnvironment):
            return LocalEnvironment(path)
        if isinstance(self._environment, DockerEnvironment):
            await self._environment.setup()
            container_id = self._environment._container_id
            if not container_id:
                raise RuntimeError("candidate container identity is unavailable")
            return DockerWorkspaceEnvironment(
                container_id=container_id,
                repo_root=path,
                command_prefix=self._environment._command_prefix,
                timeout_returncode=self._environment._timeout_returncode,
            )
        if isinstance(self._environment, VMEnvironment):
            return VMEnvironment(
                self._environment.connection, path, state_directory=self._environment.state_directory,
            )
        raise RuntimeError("candidate worktrees require a local, Docker, or VM environment")

    async def acquire(self, label: str) -> _CandidateLease:
        return await self._acquire(label)

    async def _acquire(
        self, label: str, *, strict_capture: bool = False,
        lease_observer: Callable[[_CandidateLease], None] | None = None,
        strict_untracked_exclusions: Sequence[str] = (),
    ) -> _CandidateLease:
        repository_root = await self._repository_root()
        workspace_prefix = await self._workspace_prefix()
        token = uuid.uuid4().hex
        if isinstance(self._environment, LocalEnvironment):
            path = tempfile.mkdtemp(prefix="opencollab-candidate-")
            os.rmdir(path)
        else:
            path = f"/tmp/opencollab-candidate-{token}"
        base_revision = _complete(
            await self._environment.exec_cmd(
                f"git -C {shlex.quote(repository_root)} rev-parse --verify HEAD",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            "candidate base revision",
        ).strip()
        source_owned = (
            *self._owned_untracked_paths(repository_root),
            *(_safe_path(path) for path in strict_untracked_exclusions),
        )
        source_untracked = (
            *source_owned,
            *await _mounted_recovery_paths(self._environment, repository_root),
        )
        if strict_capture:
            source_patch = await _complete_patch(
                await self._environment.exec_cmd(
                    f"cd -- {shlex.quote(repository_root)} && "
                    + guarded_staged_diff_command(
                        base_revision=base_revision,
                        reject_ignored_untracked=False,
                        exclude_untracked_paths=source_untracked,
                    ),
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "team source contents capture", self._environment, repository_root,
                base_revision, None, captured_blobs=True,
            )
        async with AsyncExitStack() as capture_resources:
            converted = await _has_worktree_conversions(
                self._environment, repository_root, strict=strict_capture,
                exclude_untracked_paths=source_untracked,
            )
            source_capture = physical_context = None
            if converted:
                if not strict_capture:
                    await _assert_patchable_submodules(
                        self._environment, repository_root, base_revision, reject_ignored_untracked=False,
                        exclude_untracked_paths=source_untracked,
                    )
                physical_context = await capture_resources.enter_async_context(_physical_git(
                    self._environment, repository_root, strict=strict_capture,
                ))
                source_capture = await _capture_physical(
                    self._environment, repository_root, base_revision,
                    exclude_paths=source_untracked,
                    strict=strict_capture, metadata=physical_context,
                )
                source_patch = source_capture.patch
            elif not strict_capture:
                source_patch = await self._source_patch(
                    repository_root, base_revision, untracked_exclusions=source_untracked,
                )
            initialized_source = await _initialized_submodule_paths(
                self._environment, repository_root, strict=strict_capture,
                source_prefix=workspace_prefix.rstrip("/"),
            )
            source_modules = await _physical_submodule_trees(
                self._environment, repository_root, strict=strict_capture,
                source_prefix=workspace_prefix.rstrip("/"),
                exclude_paths=source_untracked,
                module_paths=initialized_source,
            )
            lease = _CandidateLease(
                base_environment=self._environment,
                environment=None,
                source_workspace=repository_root,
                candidate_workspace=path,
                base_revision=base_revision,
                workspace_prefix=workspace_prefix,
                physical_base_revision=source_capture.tree if source_capture is not None else None,
                strict_capture=strict_capture,
                source_untracked_exclusions=source_owned,
                physical_source_submodules=source_modules,
                initialized_source_submodules=initialized_source,
            )
            if lease_observer is not None:
                lease_observer(lease)
            try:
                # Environment adapters terminate their command group before raising
                # cancellation. Cleanup starts only after that command has settled.
                create_command = (
                    "git -C "
                    f"{shlex.quote(repository_root)} worktree add --detach -- "
                    f"{shlex.quote(path)} {shlex.quote(base_revision)}"
                )
                if strict_capture:
                    git, guard = _git_command_and_config_guard()
                    create_command = (
                        f"cd -- {shlex.quote(repository_root)} && {guard}{git} "
                        f"worktree add --detach -- {shlex.quote(path)} {shlex.quote(base_revision)}"
                    )
                result = await self._environment.exec_cmd(
                    create_command,
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                )
                _complete(result, f"candidate worktree setup for {label}")
                candidate_workspace = path
                if workspace_prefix:
                    candidate_workspace = posixpath.join(path, _safe_path(workspace_prefix))
                    _complete(
                        await self._environment.exec_cmd(
                            f"mkdir -p -- {shlex.quote(candidate_workspace)}",
                            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                        ),
                        "candidate workspace directory",
                    )
                lease.environment = await self._candidate_environment(candidate_workspace)
                await lease.environment.setup()
                if isinstance(self._environment, LocalEnvironment):
                    source_repository = os.path.realpath(repository_root)
                    source_workspace = os.path.realpath(self._workspace)
                    try:
                        if os.path.commonpath((source_repository, source_workspace)) != source_repository:
                            raise RuntimeError("candidate source workspace is outside its Git repository")
                    except ValueError as exc:
                        raise RuntimeError("candidate source workspace is outside its Git repository") from exc

                async def git_in(workspace: str, *arguments: str) -> Any:
                    return await self._environment.exec_cmd(
                        shlex.join(("git", "-C", workspace, *arguments)),
                        timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                    )

                async def git_marker_exists(source_module: str) -> bool:
                    marker = shlex.quote(posixpath.join(source_module, ".git"))
                    result = await self._environment.exec_cmd(
                        f"test -e {marker} || test -L {marker}",
                        timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                    )
                    _complete(result, "source submodule Git metadata inspection", allowed=(0, 1))
                    return result.returncode == 0

                await _initialize_source_available_submodules(
                    repository_root,
                    path,
                    git_in=git_in,
                    source_prefix=workspace_prefix.rstrip("/"),
                    require_initialized=False,
                    local_filesystem=isinstance(self._environment, LocalEnvironment),
                    git_marker_exists=git_marker_exists,
                )
                lease.initialized_candidate_submodules = await _initialized_submodule_paths(
                    self._environment, path, strict=strict_capture,
                    source_prefix=workspace_prefix.rstrip("/"),
                )
                lease.physical_candidate_submodules = await _physical_submodule_trees(
                    self._environment, path, strict=strict_capture,
                    source_prefix=workspace_prefix.rstrip("/"),
                    include_paths=tuple(module for module, _tree in source_modules),
                    module_paths=lease.initialized_candidate_submodules,
                )
                if lease.physical_candidate_submodules != source_modules:
                    raise RuntimeError("source submodule physical contents cannot be reproduced in candidate")
                if source_patch.strip():
                    async with _candidate_temporary(
                        self._environment, repository_root, source_patch,
                        prefix=".candidate-source-", suffix=".patch",
                    ) as source_file:
                        apply_command = (
                            f"git -C {shlex.quote(path)} apply --index --binary --whitespace=nowarn "
                            f"-- {shlex.quote(source_file)}"
                        )
                        if strict_capture:
                            git, guard = _git_command_and_config_guard()
                            apply_command = (
                                f"cd -- {shlex.quote(path)} && {guard}{git} "
                                f"apply --index --binary --whitespace=nowarn -- {shlex.quote(source_file)}"
                            )
                        _complete(
                            await self._environment.exec_cmd(
                                apply_command,
                                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                            ),
                            "candidate source contents",
                        )
                    # The candidate's index records its starting contents, including
                    # source untracked files, while the source index stays untouched.
                    lease.base_revision = _complete(
                        await self._environment.exec_cmd(
                            f"git -C {shlex.quote(path)} write-tree",
                            timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                        ),
                        "candidate source tree",
                    ).strip()
                if source_capture is not None:
                    candidate_metadata = (
                        f"{physical_context[0]} -C {shlex.quote(path)} --work-tree={shlex.quote(path)} "
                        f"-c safe.directory= -c safe.directory={shlex.quote(path)}",
                        physical_context[1],
                    )
                    current_physical = await _physical_tree(
                        self._environment, path, lease.base_revision,
                        strict=strict_capture, metadata=candidate_metadata,
                    )
                    physical_source_patch = await _tree_patch(
                        self._environment, path, current_physical, source_capture.tree,
                        strict=strict_capture,
                    )
                    if physical_source_patch.strip():
                        from opencollab.patches import patch_paths

                        async with _candidate_temporary(
                            self._environment, path, physical_source_patch,
                            prefix=".candidate-physical-source-", suffix=".patch",
                        ) as physical_source_file:
                            await _apply_physical(
                                self._environment, path, physical_source_file, patch_paths(physical_source_patch),
                                strict=strict_capture, metadata=candidate_metadata,
                            )
                    _complete(await self._environment.exec_cmd(
                        f"git -C {shlex.quote(path)} update-ref refs/worktree/opencollab-physical-source "
                        f"{shlex.quote(source_capture.tree)}",
                    ), "candidate physical source reference")
                # A worktree-local ref retains the original contents for recovery
                # throughout the lease, including after commits or Git collection.
                _complete(
                    await self._environment.exec_cmd(
                        f"git -C {shlex.quote(path)} update-ref refs/worktree/opencollab-source "
                        f"{shlex.quote(lease.base_revision)}",
                        timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                    ),
                    "candidate source recovery reference",
                )
                return lease
            except BaseException as failure:
                if isinstance(failure, ProcessCleanupError) or getattr(self._environment, "revoked", False):
                    if lease.environment is not None:
                        try:
                            await await_owned_operation(lease.environment.cleanup())
                        except BaseException as cleanup_error:
                            add_exception_note(failure, f"candidate environment cleanup failed: {cleanup_error}")
                    add_exception_note(failure, f"candidate worktree retained until command cleanup completes: {path}")
                else:
                    try:
                        await await_owned_operation(lease.cleanup())
                    except BaseException as cleanup_error:
                        add_exception_note(
                            failure,
                            f"candidate initialization cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}",
                        )
                raise

    async def source_diff(self, exclude_paths: Sequence[str] = ()) -> str:
        repository_root = await self._repository_root()
        excluded = _repository_exclusions(await self._workspace_prefix(), exclude_paths) if exclude_paths else ()
        base_revision = await self.source_revision()
        return await self._source_patch(
            repository_root, base_revision, excluded,
            untracked_exclusions=await self._source_untracked_paths(repository_root),
        )

    async def physical_source_revision(self, exclude_paths: Sequence[str] = ()) -> str | None:
        """Read conversion-aware source trees with repository-relative exclusions."""
        root = await self._repository_root()
        excluded = tuple(_safe_path(path) for path in exclude_paths)
        owned = await self._source_untracked_paths(root)
        modules = await _physical_submodule_trees(
            self._environment, root, exclude_paths=(*owned, *excluded),
            exclude_tree_paths=excluded,
        )
        revision = None
        if await _has_worktree_conversions(
            self._environment, root, exclude_paths=excluded,
            exclude_untracked_paths=owned,
        ):
            revision = await _physical_tree(
                self._environment, root, await self.source_revision(),
                exclude_paths=(*owned, *excluded),
                exclude_tree_paths=excluded,
            )
        return json.dumps((revision, modules)) if modules else revision

    async def track_source(self, patch: str | None, exclude_paths: Sequence[str] = ()) -> Any:
        return await _source_snapshot(self, patch, tuple(_safe_path(path) for path in exclude_paths))

    async def _source_changed(self, exclude_paths: Sequence[str] = ()) -> bool:
        repository_root = await self._repository_root()
        excluded = (
            _repository_exclusions(await self._workspace_prefix(), exclude_paths)
            if exclude_paths else ()
        )
        owned = await self._source_untracked_paths(repository_root)

        def relative_paths(paths: Sequence[str], prefix: str) -> tuple[str, ...]:
            if not prefix:
                return tuple(paths)
            return tuple(
                "." if path == prefix or prefix.startswith(path + "/") else path[len(prefix) + 1:]
                for path in paths
                if path == prefix or prefix.startswith(path + "/") or path.startswith(prefix + "/")
            )

        async def changed_in(workspace: str, prefix: str) -> bool:
            paths = relative_paths(excluded, prefix)
            pathspec = " -- . " + " ".join(
                shlex.quote(f":(exclude,literal){path}") for path in paths
            ) if paths else ""
            ignore_modules = " --ignore-submodules=dirty" if owned or exclude_paths else ""
            status = _complete(
                await self._environment.exec_cmd(
                    f"git --no-optional-locks -C {shlex.quote(workspace)} "
                    "status --porcelain=v1 -z --untracked-files=all" + ignore_modules + pathspec,
                    timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
                ),
                "candidate source status",
            )
            module_owned = relative_paths(owned, prefix)
            owned_prefixes = tuple(path + "/" for path in module_owned)
            return any(
                record and not (
                    record.startswith("?? ")
                    and ("." in module_owned or record[3:].startswith(owned_prefixes))
                )
                for record in status.split("\0")
            )

        if await changed_in(repository_root, ""):
            return True
        if exclude_paths:
            changed = bool(_complete(await self._environment.exec_cmd(
                _submodule_status_command(
                    f"git --no-optional-locks -C {shlex.quote(repository_root)}",
                    reject_ignored_untracked=False, exclude_paths=excluded,
                    exclude_untracked_paths=owned,
                ), timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ), "candidate source submodule status").strip())
            if changed:
                return True
        elif owned:
            for path in await _initialized_submodule_paths(self._environment, repository_root):
                if await changed_in(posixpath.join(repository_root, path), path):
                    return True
        return bool(await self.source_diff(exclude_paths))

    async def source_revision(self) -> str:
        return _complete(
            await self._environment.exec_cmd(
                f"git -C {shlex.quote(await self._repository_root())} rev-parse --verify HEAD",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ),
            "candidate source revision",
        ).strip()

    async def source_index_revision(self) -> str:
        root = await self._repository_root()
        # Unmerged stages have no tree, and the entry list can exceed process capture.
        async with _candidate_temporary(
            self._environment, root, "", prefix=".candidate-source-state-",
        ) as entries:
            git = f"git -C {shlex.quote(root)}"
            return _complete(await self._environment.exec_cmd(
                f"{git} ls-files --stage -z > {shlex.quote(entries)} && "
                f"{git} hash-object --no-filters --stdin < {shlex.quote(entries)}",
                timeout=CANDIDATE_WORKSPACE_GIT_TIMEOUT_SECONDS,
            ), "candidate source index entries").strip()

    async def restore_source(self, patch: str) -> None:
        await self._replace_source_diff(patch)

    async def _replace_source_diff(self, patch: str) -> None:
        current = await self.source_diff()
        async with _candidate_temporary(
            self._environment, self._workspace, current,
            prefix=".candidate-current-", suffix=".patch", cleanup_best_effort=True,
        ) as current_file:
            async with _candidate_temporary(
                self._environment, self._workspace, patch,
                prefix=".candidate-target-", suffix=".patch", cleanup_best_effort=True,
            ) as target_file:
                reversed_current = False
                try:
                    if current.strip():
                        await self._apply(
                            current_file,
                            "candidate current-source removal",
                            reverse=True,
                        )
                        reversed_current = True
                    if patch.strip():
                        await self._apply(target_file, "candidate source restoration")
                except BaseException as failure:
                    if reversed_current:
                        try:
                            await self._apply(
                                current_file,
                                "candidate source restoration rollback",
                            )
                        except BaseException as rollback:
                            raise RuntimeError(
                                "candidate source restoration and rollback both failed"
                            ) from rollback
                    raise failure

    async def _apply(self, path: str, operation: str, *, reverse: bool = False) -> None:
        reverse_flag = " --reverse" if reverse else ""
        repository_root = await self._repository_root()
        result = await self._environment.exec_cmd(
            "git -c core.filemode=true -C "
            f"{shlex.quote(repository_root)} apply --binary --whitespace=nowarn"
            f"{reverse_flag} -- {shlex.quote(path)}",
            timeout=120,
        )
        _complete(result, operation)

    async def adopt(
        self,
        patch: str,
        preserve_paths: Sequence[str] = (),
    ) -> None:
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("candidate patch must be non-empty")
        preserved = (
            _repository_exclusions(await self._workspace_prefix(), preserve_paths)
            if preserve_paths else ()
        )
        await _apply_increment(
            self._environment, await self._repository_root(), patch, preserved,
        )

    async def adopt_run(self, candidate: Any, preserve_paths: Sequence[str] = ()) -> None:
        if candidate.delivery_diff is None or candidate.delivery_source_diff != candidate.diff:
            await self.adopt(candidate.diff, preserve_paths)
            return
        patch = candidate.effective_diff
        if not patch.strip():
            return
        preserved = (
            _repository_exclusions(await self._workspace_prefix(), preserve_paths)
            if preserve_paths else ()
        )
        root = await self._repository_root()
        await _apply_increment(
            self._environment, root, patch, preserved,
            physical_paths=tuple(dict.fromkeys((
                *candidate.delivery_paths,
                *await _physical_paths(self._environment, root, patch),
            ))),
        )

__all__ = ["EnvCandidateWorkspace"]
