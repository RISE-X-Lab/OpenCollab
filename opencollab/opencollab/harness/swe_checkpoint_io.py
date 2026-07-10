from __future__ import annotations

import hashlib
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollab.adapters.env import ExecResult
from opencollab.adapters.safe_files import (
    read_regular_bytes,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)


def _checkpoint_module():
    return sys.modules["opencollab.harness.swe_checkpoint"]


def _atomic_write(
    path: Path,
    text: str,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    payload = text.encode("utf-8")
    max_bytes = (
        _checkpoint_module().MAX_CHECKPOINT_PATCH_BYTES
        if path.name == _checkpoint_module().CHECKPOINT_PATCH
        else _checkpoint_module().MAX_CHECKPOINT_META_BYTES
    )
    write_regular_bytes_atomic(
        path,
        payload,
        max_bytes=max_bytes,
        expected_parent_identity=expected_parent_identity,
    )


def _unlink_durable(
    path: Path,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    unlink_regular_file_durable(
        path,
        expected_parent_identity=expected_parent_identity,
    )


def _read_bounded_text(
    path: Path,
    *,
    max_bytes: int,
    errors: str = "replace",
    expected_parent_identity: tuple[int, int] | None = None,
) -> str:
    payload = read_regular_bytes(
        path,
        max_bytes=max_bytes,
        expected_parent_identity=expected_parent_identity,
    )
    return payload.decode("utf-8", errors=errors)


def _patch_sha(patch: str) -> str:
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def _truncated_output_error(result: ExecResult, *, label: str) -> str:
    parts: list[str] = []
    if result.stdout_truncated:
        parts.append(f"stdout dropped {result.stdout_dropped_bytes} bytes")
    if result.stderr_truncated:
        parts.append(f"stderr dropped {result.stderr_dropped_bytes} bytes")
    return f"{label} output truncated: {', '.join(parts)}" if parts else ""


def worktree_diff_command(exclude_paths: Sequence[str] = ()) -> str:
    resets = ""
    for path in exclude_paths:
        if not str(path).strip():
            continue
        resets += f'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q HEAD -- {shlex.quote(str(path))} && '
    return (
        'tmpdir=$(mktemp -d) || exit 125; idx="$tmpdir/index"; '
        "trap 'rm -rf -- \"$tmpdir\"' EXIT; "
        'GIT_INDEX_FILE="$idx" git read-tree HEAD && '
        'GIT_INDEX_FILE="$idx" git add -A && '
        f"{resets}"
        'GIT_INDEX_FILE="$idx" git diff --cached --binary HEAD'
    )


@dataclass(frozen=True)
class CheckpointResult:
    status: str
    patch_bytes: int = 0
    patch_sha256: str = ""
    reason: str = ""
    error: str = ""
    preserved_previous_patch: bool = False
    submission_eligible: bool = False
    worktree_integrity_proven: bool = True
    background_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "patch_bytes": self.patch_bytes,
            "patch_sha256": self.patch_sha256,
            "reason": self.reason,
            "error": self.error,
            "preserved_previous_patch": self.preserved_previous_patch,
            "submission_eligible": self.submission_eligible,
            "worktree_integrity_proven": self.worktree_integrity_proven,
            "background_errors": list(self.background_errors),
        }


def _checkpoint_meta_integrity_error(
    meta: dict[str, Any] | None,
    *,
    patch_bytes: int,
    patch_sha256: str,
) -> str | None:
    if not isinstance(meta, dict):
        return "checkpoint metadata is missing or invalid"
    if meta.get("schema") != "opencollab.swe_worktree_checkpoint.v1":
        return "checkpoint metadata schema is invalid"
    if meta.get("status") not in {"written", "failed"}:
        return "checkpoint metadata status is invalid for a stored patch"
    if str(meta.get("patch_sha256") or "") != patch_sha256:
        return "checkpoint patch checksum does not match metadata"
    if meta.get("patch_bytes") != patch_bytes:
        return "checkpoint patch byte count does not match metadata"
    if not isinstance(meta.get("submission_eligible"), bool):
        return "checkpoint metadata submission eligibility is invalid"
    return None
