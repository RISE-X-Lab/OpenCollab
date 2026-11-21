"""Atomic application of complete candidate patch increments."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from opencollab.adapters._candidate_adoption import _CHECK_PARENTS, _adoption_command, _adoption_transaction
from opencollab.adapters._candidate_physical import _physical_git
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary
from opencollab.adapters.git_patch import _git_command_and_config_guard
from opencollab.adapters.tools.base import host_write_locks
from opencollab.patches import patch_paths


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("candidate preserve path must be non-empty text")
    normalized = posixpath.normpath(value)
    if posixpath.isabs(normalized) or normalized == ".." or normalized.startswith("../"):
        raise ValueError("candidate preserve path escapes the repository")
    return normalized


def _worktree_attributes(workspace: str, paths: Sequence[str]) -> set[str]:
    directories = {""}
    for path in paths:
        while (path := posixpath.dirname(path)) not in directories:
            directories.add(path)
    return {posixpath.join(workspace, path, ".gitattributes") for path in directories}


@asynccontextmanager
async def _candidate_write_locks(
    environment: Any, workspace: str, paths: Sequence[str], *,
    repository: bool, in_place: bool, read_attributes_file: Callable[[], Awaitable[str | None]] | None,
) -> AsyncIterator[str | None]:
    delegate = getattr(environment, "_delegate", environment)
    docker = isinstance(delegate, DockerEnvironment)
    local = getattr(environment, "local_filesystem", False)
    lock_root = workspace if docker or local else getattr(environment, "workspace", workspace)
    publication_lock = posixpath.join(lock_root, ".candidate-adoption")
    patch_targets = {posixpath.join(workspace, path) for path in paths}
    prefer_shared_ancestors = in_place and bool(paths) and (
        docker or local or getattr(environment, "_handles_file_lock_hierarchy", False)
    )
    lock_attributes = prefer_shared_ancestors and repository
    workspace_prefix = posixpath.normpath(workspace).rstrip("/") + "/"

    async def read_attribute_paths() -> tuple[str | None, str | None]:
        configured = await read_attributes_file() if read_attributes_file is not None else None
        referent = None
        if lock_attributes and configured not in (None, "/dev/null"):
            if posixpath.normpath(configured).startswith(workspace_prefix):
                resolved = _complete(await environment.exec_cmd(
                    f"readlink -f -- {shlex.quote(configured)} 2>/dev/null || true", timeout=120,
                ), "candidate attribute path resolution").removesuffix("\n")
                referent = resolved if posixpath.isabs(resolved) else None
        return configured, referent

    attribute_paths = await read_attribute_paths() if lock_attributes else (None, None)
    while True:
        targets = patch_targets.copy()
        share_ancestors = prefer_shared_ancestors
        if lock_attributes:
            targets.update(_worktree_attributes(workspace, paths))
            for path in attribute_paths:
                if path in (None, "/dev/null"):
                    continue
                normalized = posixpath.normpath(path)
                if normalized.startswith(workspace_prefix):
                    targets.add(normalized)
                else:
                    share_ancestors = False
        ordered = (publication_lock, *sorted(targets - {publication_lock}))
        locks = (
            delegate.file_write_locks(ordered, _repository_root=workspace, _shared_ancestors=share_ancestors)
            if docker else host_write_locks(ordered, environment, shared_ancestors=share_ancestors)
        )
        async with locks:
            current = await read_attribute_paths()
            if lock_attributes and current != attribute_paths:
                # Git configuration or its symlink can change while acquisition waits.
                attribute_paths = current
                continue
            if share_ancestors:
                # Git reports the patch's intent. Check the actual files after taking the locks.
                inspection = (
                    f"cd -- {shlex.quote(workspace)} && workspace=$(pwd -P && printf .) || exit 2\n"
                    'workspace=${workspace%??}\nconflict() { return 1; }\n' + _CHECK_PARENTS
                    + f"for path in {shlex.join(paths)}; do live=\"$workspace/$path\"; "
                    'check_parents "$live" && [ -f "$live" ] && [ ! -L "$live" ] || exit 1; done'
                )
                async with _adoption_command(environment, workspace, inspection, repository=repository) as command:
                    result = await environment.exec_cmd(command, timeout=120)
                _complete(result, "candidate existing-file inspection", allowed=(0, 1))
                if result.returncode == 1:
                    prefer_shared_ancestors = False
                    continue
            yield current[0]
            return


async def _apply_increment(
    environment: Any, workspace: str, patch: str,
    preserve_paths: Sequence[str] = (), *, repository: bool = True, strict: bool = False,
    physical_paths: Sequence[str] = (),
) -> None:
    """Apply one complete increment atomically without changing the real index."""
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("candidate patch must be non-empty")
    preserved = tuple(_safe_path(path) for path in preserve_paths)
    # Directory copies can sit below an unrelated Git worktree.
    git_prefix = "git" if repository else "GIT_DIR=/dev/null git"
    async with _patch_temporary(
        environment, workspace, patch, prefix=".candidate-adopt-", suffix=".patch",
        cleanup_best_effort=True, repository=repository,
    ) as candidate_file:
        output = _complete(await environment.exec_cmd(
            f"{git_prefix} -C {shlex.quote(workspace)} apply --numstat -z --summary -- {shlex.quote(candidate_file)}",
            timeout=30,
        ), "candidate path inspection")
        stats, separator, summary = output.rpartition("\0")
        candidate_paths = {
            line.split("\t", 2)[-1] for line in (stats if separator else output).split("\0")
            if line.count("\t") >= 2
        }
        candidate_paths.update(patch_paths(patch))
        if any(
            protected == "." or path == protected or path.startswith(protected + "/")
            for protected in preserved for path in candidate_paths
        ):
            raise ValueError("candidate patch overlaps a preserved path")
        paths = sorted(candidate_paths)
        for path in paths:
            if (
                not path or "\0" in path or posixpath.normpath(path) != path or posixpath.isabs(path)
                or path in {".", ".."} or path.startswith("../")
                or any(part.casefold() == ".git" for part in path.split("/"))
            ):
                raise RuntimeError(f"invalid path in candidate patch: {path!r}")
        git = f"{git_prefix} -c core.filemode=true"
        guard = ""
        if strict and repository:
            git, guard = _git_command_and_config_guard()
        repository_git = f"cd -- {shlex.quote(workspace)} && {guard}{git}"

        async def read_attributes_file() -> str | None:
            configured = _complete(await environment.exec_cmd(
                f"{repository_git} config --path --get core.attributesFile", timeout=120,
            ), "candidate attribute configuration", allowed=(0, 1)).removesuffix("\n")
            return posixpath.join(workspace, configured) if configured else None

        configured_rules = repository and not strict and not physical_paths
        async with _candidate_write_locks(
            environment, workspace, paths, repository=repository, in_place=bool(separator and not summary.strip()),
            read_attributes_file=read_attributes_file if configured_rules else None,
        ) as attributes_file:
            command = repository_git
            if attributes_file is not None:
                command += f" -c core.attributesFile={shlex.quote(attributes_file)}"
            async with AsyncExitStack() as resources:
                operation = "candidate adoption"
                if physical_paths and repository:
                    if guard:
                        _complete(await environment.exec_cmd(
                            f"cd -- {shlex.quote(workspace)} && {guard}:", timeout=120,
                        ), "physical candidate configuration inspection")
                    command, _ = await resources.enter_async_context(_physical_git(
                        environment, workspace, physical_paths, strict=strict,
                    ))
                    operation = "physical candidate adoption"
                # Let Git reject unsafe path aliases before a recursive backup can
                # encounter repository metadata. Publication can still fail later.
                check = f"{command} apply --check --binary --whitespace=nowarn -- {shlex.quote(candidate_file)}"
                _complete(await environment.exec_cmd(check, timeout=120), operation)

                async def replay(
                    staged: str, git_directory: str | None, saved_attributes_file: str | None,
                ) -> None:
                    async with AsyncExitStack() as metadata:
                        replay_git = repository_git
                        if physical_paths and repository:
                            replay_git, git_directory = await metadata.enter_async_context(_physical_git(
                                environment, workspace, physical_paths, strict=strict,
                            ))
                        if git_directory is not None:
                            replay_git += (
                                f" --git-dir={shlex.quote(git_directory)} --work-tree={shlex.quote(staged)}"
                                f" -c safe.directory={shlex.quote(staged)}"
                            )
                        if saved_attributes_file is not None:
                            replay_git += f" -c core.attributesFile={shlex.quote(saved_attributes_file)}"
                        replay_git += f" -C {shlex.quote(staged)}"
                        _complete(await environment.exec_cmd(
                            f"{replay_git} apply --whitespace=nowarn --check --binary"
                            f" -- {shlex.quote(candidate_file)}", timeout=120,
                        ), "candidate recovery replay check")
                        # A failed replay still records the files Git was able to write.
                        result = await environment.exec_cmd(
                            f"{replay_git} apply --whitespace=nowarn --binary"
                            f" -- {shlex.quote(candidate_file)}", timeout=120,
                        )
                        _complete(result, "candidate recovery replay", allowed=(0, 128))

                async with _adoption_transaction(
                    environment, workspace, paths, repository=repository,
                    replay=replay, attributes_file=attributes_file,
                ):
                    try:
                        _complete(await environment.exec_cmd(
                            f"{command} apply --binary --whitespace=nowarn -- {shlex.quote(candidate_file)}",
                            timeout=120,
                        ), operation)
                    finally:
                        await resources.aclose()
