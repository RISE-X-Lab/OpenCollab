"""Atomic application of complete candidate patch increments."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any

from opencollab.adapters._candidate_adoption import _adoption_transaction
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

async def _apply_increment(
    environment: Any, workspace: str, patch: str,
    preserve_paths: Sequence[str] = (), *, repository: bool = True, strict: bool = False,
    physical_paths: Sequence[str] = (),
) -> None:
    """Apply one complete increment atomically without changing the real index."""
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("candidate patch must be non-empty")
    preserved = tuple(_safe_path(path) for path in preserve_paths)
    delegate = getattr(environment, "_delegate", environment)
    docker = isinstance(delegate, DockerEnvironment)
    lock_root = (
        workspace if docker or getattr(environment, "local_filesystem", False)
        else getattr(environment, "workspace", workspace)
    )
    # Directory copies can sit below an unrelated Git worktree.
    git_prefix = "git" if repository else "GIT_DIR=/dev/null git"
    async with _patch_temporary(
        environment, workspace, patch, prefix=".candidate-adopt-", suffix=".patch",
        cleanup_best_effort=True, repository=repository,
    ) as candidate_file:
        output = _complete(await environment.exec_cmd(
            f"{git_prefix} -C {shlex.quote(workspace)} apply --numstat -z -- {shlex.quote(candidate_file)}",
            timeout=30,
        ), "candidate path inspection")
        candidate_paths = {
            line.split("\t", 2)[-1] for line in output.split("\0") if line.count("\t") >= 2
        }
        candidate_paths.update(patch_paths(patch))
        if any(
            protected == "." or path == protected or path.startswith(protected + "/")
            for protected in preserved for path in candidate_paths
        ):
            raise ValueError("candidate patch overlaps a preserved path")
        targets = (posixpath.join(lock_root, ".candidate-adoption"),
                   *(posixpath.join(workspace, path) for path in sorted(candidate_paths)))
        locks = (
            delegate.file_write_locks(targets, _repository_root=workspace)
            if docker else host_write_locks(targets, environment)
        )
        async with locks:
            git = f"{git_prefix} -c core.filemode=true"
            guard = ""
            if strict and repository:
                git, guard = _git_command_and_config_guard()
            async with AsyncExitStack() as resources:
                command = f"cd -- {shlex.quote(workspace)} && {guard}{git}"
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
                configured_rules = repository and not strict and not physical_paths
                if configured_rules:
                    check += f' && ({git} config --path --get core.attributesFile || [ "$?" -eq 1 ])'
                checked = _complete(await environment.exec_cmd(check, timeout=120), operation)
                attributes_file = None
                if configured_rules and (configured := checked.removesuffix("\n")):
                    attributes_file = posixpath.join(workspace, configured)

                async def replay(
                    staged: str, git_directory: str | None, saved_attributes_file: str | None,
                ) -> None:
                    async with AsyncExitStack() as metadata:
                        replay_git = f"cd -- {shlex.quote(workspace)} && {guard}{git}"
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
                    environment, workspace, sorted(candidate_paths), repository=repository,
                    replay=replay, attributes_file=attributes_file,
                ):
                    try:
                        _complete(await environment.exec_cmd(
                            f"{command} apply --binary --whitespace=nowarn -- {shlex.quote(candidate_file)}",
                            timeout=120,
                        ), operation)
                    finally:
                        await resources.aclose()
