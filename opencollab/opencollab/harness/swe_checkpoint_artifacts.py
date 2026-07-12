"""Artifact metadata and path handling for SWE worktree checkpoints."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Sequence


def build_checkpoint_meta(
    *,
    status: str,
    reason: str,
    interval_seconds: float,
    patch_path: Path,
    patch_bytes: int = 0,
    patch_sha256: str = "",
    error: str = "",
    preserved_previous_patch: bool = False,
    submission_eligible: bool = False,
) -> dict[str, Any]:
    """Build the durable metadata record stored beside a checkpoint patch."""
    return {
        "schema": "opencollab.swe_worktree_checkpoint.v1",
        "status": status,
        "reason": reason,
        "captured_at": time.time(),
        "checkpoint_interval_seconds": interval_seconds,
        "loss_bound_seconds": interval_seconds,
        "patch_path": str(patch_path),
        "patch_bytes": patch_bytes,
        "patch_sha256": patch_sha256,
        "error": error,
        "preserved_previous_patch": preserved_previous_patch,
        "submission_eligible": submission_eligible,
    }


def checkpoint_artifact_exclude_paths(
    env: Any,
    artifact_paths: Sequence[Path],
) -> tuple[str, ...]:
    """Return checkpoint artifact paths relative to an environment workspace."""
    host_workspace = (
        env.workspace
        if env.local_filesystem
        else getattr(env, "host_workspace", None)
    )
    if not host_workspace:
        return ()
    try:
        lexical_root = Path(os.path.abspath(os.fspath(host_workspace)))
    except (OSError, TypeError, ValueError):
        return ()
    root_candidates = [lexical_root]
    try:
        resolved_root = lexical_root.resolve(strict=False)
        if resolved_root not in root_candidates:
            root_candidates.append(resolved_root)
    except (OSError, RuntimeError):
        pass

    paths: dict[str, None] = {}
    for artifact_path in artifact_paths:
        try:
            lexical_artifact = Path(os.path.abspath(os.fspath(artifact_path)))
        except (OSError, TypeError, ValueError):
            continue
        artifact_candidates = [lexical_artifact]
        try:
            resolved_artifact = lexical_artifact.resolve(strict=False)
            if resolved_artifact not in artifact_candidates:
                artifact_candidates.append(resolved_artifact)
        except (OSError, RuntimeError):
            pass
        for candidate in artifact_candidates:
            for root in root_candidates:
                try:
                    rel = candidate.relative_to(root)
                except ValueError:
                    continue
                if rel != Path("."):
                    paths.setdefault(rel.as_posix(), None)
    return tuple(paths)
