"""Bounded-loss worktree checkpoints for long SWE-bench runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from opencollab.adapters.env import Environment

CHECKPOINT_PATCH = "checkpoint.worktree.patch"
CHECKPOINT_META = "checkpoint.worktree.json"
ENV_RECOVERY_PATCH_PREFIX = "/tmp/opencollab-checkpoint-recovery-"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _patch_sha(patch: str) -> str:
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def worktree_diff_command(exclude_paths: Sequence[str] = ()) -> str:
    resets = ""
    for path in exclude_paths:
        if not str(path).strip():
            continue
        resets += f'GIT_INDEX_FILE="$idx" git reset -q HEAD -- {shlex.quote(str(path))} >/dev/null 2>&1 || true; '
    return (
        'idx=$(mktemp); trap \'rm -f "$idx"\' EXIT; '
        'git read-tree --index-output="$idx" HEAD && '
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
            "background_errors": list(self.background_errors),
        }


class WorktreeCheckpoint:
    """Periodic host-side checkpoint of the env worktree diff.

    The checkpoint captures a submittable worktree patch, not model state. With
    a 300-second interval, a crash can lose the current in-flight model/tool
    turn plus at most one checkpoint interval of saved file edits.
    """

    def __init__(self, run_dir: Path, *, interval_seconds: float = 300.0) -> None:
        self.run_dir = Path(run_dir)
        self.interval_seconds = float(interval_seconds)
        self.patch_path = self.run_dir / CHECKPOINT_PATCH
        self.meta_path = self.run_dir / CHECKPOINT_META
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._background_errors: list[str] = []

    async def capture(
        self,
        env: Environment,
        *,
        reason: str,
        exclude_paths: Sequence[str] = (),
    ) -> CheckpointResult:
        exclude_paths = tuple(exclude_paths) + self._artifact_exclude_paths(env)
        try:
            result = await env.exec_cmd(worktree_diff_command(exclude_paths), timeout=120)
        except Exception as exc:  # noqa: BLE001
            return self._write_failure(reason=reason, error=f"{type(exc).__name__}: {exc}")
        if result.returncode != 0:
            return self._write_failure(reason=reason, error=result.stderr[:1000])

        patch = result.stdout
        patch_bytes = len(patch.encode("utf-8", errors="surrogatepass"))
        patch_sha = _patch_sha(patch)
        preserved = self._has_existing_patch()
        if patch.strip():
            try:
                _atomic_write(self.patch_path, patch)
            except Exception as exc:  # noqa: BLE001
                return self._write_failure(reason=reason, error=f"{type(exc).__name__}: {exc}")
            preserved = False
        meta = self._meta(
            "written" if patch.strip() else "empty",
            reason=reason,
            patch_bytes=patch_bytes,
            patch_sha256=patch_sha,
            preserved_previous_patch=preserved and not patch.strip(),
            submission_eligible=bool(patch.strip()),
        )
        self._write_meta(meta)
        return CheckpointResult(
            status=str(meta["status"]),
            patch_bytes=patch_bytes,
            patch_sha256=patch_sha,
            reason=reason,
            preserved_previous_patch=bool(meta["preserved_previous_patch"]),
            submission_eligible=bool(meta["submission_eligible"]),
        )

    async def restore_latest(self, env: Environment) -> CheckpointResult:
        if not self.patch_path.exists():
            return CheckpointResult(status="missing", reason="restore")
        patch = self.patch_path.read_text(encoding="utf-8", errors="replace")
        if not patch.strip():
            return CheckpointResult(status="empty", reason="restore")
        meta = self._load_meta()
        if isinstance(meta, dict) and meta.get("submission_eligible") is False:
            return CheckpointResult(
                status="skipped_not_submission_eligible",
                patch_bytes=len(patch.encode("utf-8", errors="surrogatepass")),
                patch_sha256=_patch_sha(patch),
                reason="restore",
                preserved_previous_patch=bool(meta.get("preserved_previous_patch")),
                submission_eligible=False,
            )
        precheck = await env.exec_cmd(worktree_diff_command(self._artifact_exclude_paths(env)), timeout=120)
        if precheck.returncode != 0:
            return CheckpointResult(
                status="failed",
                reason="restore",
                error=precheck.stderr[:1000],
            )
        if precheck.stdout.strip():
            return CheckpointResult(
                status="skipped_dirty_worktree",
                patch_bytes=len(patch.encode("utf-8", errors="surrogatepass")),
                patch_sha256=_patch_sha(patch),
                reason="restore",
                error="worktree already has changes",
            )
        recovery_patch_path = self._env_recovery_patch_path()
        await env.write_file(recovery_patch_path, patch)
        result = await env.exec_cmd(
            f"git apply --binary --whitespace=nowarn {shlex.quote(recovery_patch_path)}",
            timeout=120,
        )
        patch_bytes = len(patch.encode("utf-8", errors="surrogatepass"))
        patch_sha = _patch_sha(patch)
        if result.returncode == 0:
            return CheckpointResult(
                status="restored",
                patch_bytes=patch_bytes,
                patch_sha256=patch_sha,
                reason="restore",
                submission_eligible=True,
            )
        return CheckpointResult(
            status="failed",
            patch_bytes=patch_bytes,
            patch_sha256=patch_sha,
            reason="restore",
            error=result.stderr[:1000],
        )

    async def start(self, env: Environment, *, exclude_paths: Sequence[str] = ()) -> None:
        if self.interval_seconds <= 0:
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(env, exclude_paths=tuple(exclude_paths)))

    async def stop(self, env: Environment, *, exclude_paths: Sequence[str] = ()) -> CheckpointResult:
        if self._task is not None:
            self._stop.set()
            try:
                await self._task
            except Exception as exc:  # noqa: BLE001
                self._background_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                self._task = None
        result = await self.capture(env, reason="final", exclude_paths=exclude_paths)
        if self._background_errors:
            return CheckpointResult(
                status=result.status,
                patch_bytes=result.patch_bytes,
                patch_sha256=result.patch_sha256,
                reason=result.reason,
                error=result.error,
                preserved_previous_patch=result.preserved_previous_patch,
                submission_eligible=result.submission_eligible,
                background_errors=tuple(self._background_errors),
            )
        return result

    async def _run(self, env: Environment, *, exclude_paths: Sequence[str]) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                return
            except asyncio.TimeoutError:
                result = await self.capture(env, reason="periodic", exclude_paths=exclude_paths)
                if result.error:
                    self._background_errors.append(result.error)

    def _meta(
        self,
        status: str,
        *,
        reason: str,
        patch_bytes: int = 0,
        patch_sha256: str = "",
        error: str = "",
        preserved_previous_patch: bool = False,
        submission_eligible: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema": "opencollab.swe_worktree_checkpoint.v1",
            "status": status,
            "reason": reason,
            "captured_at": time.time(),
            "checkpoint_interval_seconds": self.interval_seconds,
            "loss_bound_seconds": self.interval_seconds,
            "patch_path": str(self.patch_path),
            "patch_bytes": patch_bytes,
            "patch_sha256": patch_sha256,
            "error": error,
            "preserved_previous_patch": preserved_previous_patch,
            "submission_eligible": submission_eligible,
        }

    def _has_existing_patch(self) -> bool:
        try:
            return bool(self.patch_path.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            return False

    def _artifact_exclude_paths(self, env: Environment) -> tuple[str, ...]:
        workspace = getattr(env, "workspace", "") or ""
        try:
            root = Path(workspace).resolve(strict=False)
        except OSError:
            return ()

        paths: list[str] = []
        for artifact_path in (self.patch_path, self.meta_path):
            try:
                rel = artifact_path.resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                continue
            paths.append(rel.as_posix())
        return tuple(paths)

    def _load_meta(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.meta_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _env_recovery_patch_path(self) -> str:
        digest = hashlib.sha256(str(self.run_dir.resolve(strict=False)).encode()).hexdigest()[:16]
        return f"{ENV_RECOVERY_PATCH_PREFIX}{digest}.patch"

    def _write_meta(self, meta: dict[str, Any]) -> None:
        try:
            _atomic_write(self.meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass

    def _write_failure(self, *, reason: str, error: str) -> CheckpointResult:
        preserved = self._has_existing_patch()
        meta = self._meta(
            "failed",
            reason=reason,
            error=error,
            preserved_previous_patch=preserved,
            submission_eligible=False,
        )
        self._write_meta(meta)
        return CheckpointResult(
            status="failed",
            reason=reason,
            error=error,
            preserved_previous_patch=preserved,
            submission_eligible=False,
        )
