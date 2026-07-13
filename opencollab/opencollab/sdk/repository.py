"""Stable repository inspection and patch-command helpers."""

from __future__ import annotations

from collections.abc import Sequence

from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.adapters.repo_map import MAX_MAP_CHARS, MAX_MAP_DEPTH, build_repo_map_via_env
from opencollab.adapters.working_tree import EnvWorkingTreeProbe

from .environment import ExecutionEnvironment

WorkingTreeProbe = EnvWorkingTreeProbe


async def build_repository_map(
    environment: ExecutionEnvironment,
    *,
    max_depth: int = MAX_MAP_DEPTH,
    max_chars: int = MAX_MAP_CHARS,
) -> str:
    """Build a bounded path map through the environment boundary."""
    return await build_repo_map_via_env(
        environment,
        max_depth=max_depth,
        max_chars=max_chars,
    )


def build_patch_command(
    *,
    base_revision: str = "HEAD",
    exclude_paths: Sequence[str] = (),
) -> str:
    """Build a fail-closed command that extracts the staged candidate patch."""
    return guarded_staged_diff_command(
        base_revision=base_revision,
        exclude_paths=exclude_paths,
    )


__all__ = [
    "WorkingTreeProbe",
    "build_patch_command",
    "build_repository_map",
]
