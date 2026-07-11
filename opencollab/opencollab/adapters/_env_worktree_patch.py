"""Pinned patch-source capture and extraction for worktree environments."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Awaitable, Callable

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters.git_patch import guarded_staged_diff_command

_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


async def capture_worktree_patch_source(
    run_git: Callable[..., Awaitable[ExecResult]],
    source: str,
) -> tuple[str, str]:
    base = await run_git("rev-parse", "--verify", "HEAD^{commit}")
    if base.stdout_truncated or base.stderr_truncated:
        raise RuntimeError("git base commit output was truncated")
    if base.returncode != 0:
        raise RuntimeError(f"cannot record worktree base commit: {base.stderr.strip()}")
    base_oid = base.stdout.strip().lower()
    if _OID_RE.fullmatch(base_oid) is None:
        raise RuntimeError("worktree base commit is not an exact object id")
    objects = await run_git(
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "objects",
    )
    if objects.returncode != 0 or objects.stdout_truncated or objects.stderr_truncated:
        raise RuntimeError("cannot record worktree object directory")
    object_path = objects.stdout.strip()
    if not os.path.isabs(object_path):
        object_path = os.path.join(source, object_path)
    object_path = os.path.realpath(object_path)
    if not stat.S_ISDIR(os.stat(object_path, follow_symlinks=False).st_mode):
        raise RuntimeError("worktree object directory is invalid")
    return base_oid, object_path


async def collect_worktree_patch(
    local_env,
    *,
    base_oid: str,
    object_directory: str,
    workspace: str,
) -> str:
    snapshot = await local_env.registered_retirement_snapshot()
    result = await local_env.exec_cmd(
        guarded_staged_diff_command(
            base_revision=base_oid,
            registered_retirement_paths=tuple(item.relative_path for item in snapshot),
            retirement_snapshot=snapshot,
            object_directory=object_directory,
            working_tree=workspace,
        )
    )
    if tuple(await local_env.registered_retirement_snapshot()) != tuple(snapshot):
        raise RuntimeError("retirement artifacts changed during patch extraction")
    if result.returncode != 0:
        raise RuntimeError(f"cannot collect worktree diff: {result.stderr.strip()}")
    if result.stdout_truncated:
        raise RuntimeError("worktree diff exceeded capture limit")
    return result.stdout


__all__ = ["capture_worktree_patch_source", "collect_worktree_patch"]
