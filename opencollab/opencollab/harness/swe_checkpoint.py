"""Bounded-loss worktree checkpoints for long SWE-bench runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from opencollab.adapters.env import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    Environment,
    ExecResult,
)
from opencollab.adapters.safe_files import (
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
)

CHECKPOINT_PATCH = "checkpoint.worktree.patch"
CHECKPOINT_META = "checkpoint.worktree.json"
DEFAULT_CHECKPOINT_ABORT_TIMEOUT = 2.0
MAX_FORCED_CHECKPOINT_ABORT_TIMEOUT = 2.0
MAX_CHECKPOINT_PATCH_BYTES = PROCESS_OUTPUT_CAPTURE_BYTES + 64 * 1024
MAX_CHECKPOINT_META_BYTES = 1024 * 1024
MAX_CHECKPOINT_TEMP_CLEANUP_SECONDS = 10.0
MAX_FAILED_RESTORE_PROOF_SECONDS = 10.0


def _atomic_write(
    path: Path,
    text: str,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    payload = text.encode("utf-8")
    max_bytes = (
        MAX_CHECKPOINT_PATCH_BYTES
        if path.name == CHECKPOINT_PATCH
        else MAX_CHECKPOINT_META_BYTES
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
        resets += (
            'GIT_INDEX_FILE="$idx" git --literal-pathspecs reset -q HEAD -- '
            f"{shlex.quote(str(path))} && "
        )
    return (
        'tmpdir=$(mktemp -d) || exit 125; idx="$tmpdir/index"; '
        'trap \'rm -rf -- "$tmpdir"\' EXIT; '
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


class WorktreeCheckpoint:
    """Periodic host-side checkpoint of the env worktree diff.

    The checkpoint captures a submittable worktree patch, not model state. With
    a 300-second interval, a crash can lose the current in-flight model/tool
    turn plus at most one checkpoint interval of saved file edits.
    """

    def __init__(self, run_dir: Path, *, interval_seconds: float = 300.0) -> None:
        if isinstance(interval_seconds, bool):
            raise ValueError("checkpoint interval must be finite and non-negative")
        try:
            normalized_interval = float(interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "checkpoint interval must be finite and non-negative"
            ) from exc
        if not math.isfinite(normalized_interval) or normalized_interval < 0:
            raise ValueError("checkpoint interval must be finite and non-negative")
        self.run_dir = Path(os.path.abspath(run_dir))
        self._run_dir_identity: tuple[int, int] | None = None
        self.interval_seconds = normalized_interval
        self.patch_path = self.run_dir / CHECKPOINT_PATCH
        self.meta_path = self.run_dir / CHECKPOINT_META
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._owned_operations: set[asyncio.Task[Any]] = set()
        self._background_errors: list[str] = []

    async def capture(
        self,
        env: Environment,
        *,
        reason: str,
        exclude_paths: Sequence[str] = (),
    ) -> CheckpointResult:
        try:
            self._bind_run_directory()
        except Exception as exc:
            return CheckpointResult(
                status="failed",
                reason=reason,
                error=f"checkpoint run directory is unsafe: {type(exc).__name__}: {exc}",
                submission_eligible=False,
            )
        exclude_paths = tuple(exclude_paths) + self._artifact_exclude_paths(env)
        try:
            result = await env.exec_cmd(worktree_diff_command(exclude_paths), timeout=120)
        except Exception as exc:  # noqa: BLE001
            return self._write_failure(reason=reason, error=f"{type(exc).__name__}: {exc}")
        truncation_error = _truncated_output_error(result, label="worktree diff")
        if truncation_error:
            return self._write_failure(reason=reason, error=truncation_error)
        if result.returncode != 0:
            return self._write_failure(reason=reason, error=result.stderr[:1000])

        patch = result.stdout
        patch_bytes = len(patch.encode("utf-8", errors="surrogatepass"))
        if patch_bytes > MAX_CHECKPOINT_PATCH_BYTES:
            return self._write_failure(
                reason=reason,
                error=(
                    "worktree diff exceeds checkpoint bound: "
                    f"{patch_bytes} > {MAX_CHECKPOINT_PATCH_BYTES} bytes"
                ),
            )
        patch_sha = _patch_sha(patch)
        preserved = self._has_existing_patch()
        previous_patch: str | None = None
        if patch.strip():
            try:
                try:
                    previous_patch = _read_bounded_text(
                        self.patch_path,
                        max_bytes=MAX_CHECKPOINT_PATCH_BYTES,
                        expected_parent_identity=self._run_dir_identity,
                    )
                except FileNotFoundError:
                    previous_patch = None
                _atomic_write(
                    self.patch_path,
                    patch,
                    expected_parent_identity=self._run_dir_identity,
                )
            except Exception as exc:  # noqa: BLE001
                write_error = f"{type(exc).__name__}: {exc}"
                try:
                    if previous_patch is None:
                        _unlink_durable(
                            self.patch_path,
                            expected_parent_identity=self._run_dir_identity,
                        )
                    else:
                        _atomic_write(
                            self.patch_path,
                            previous_patch,
                            expected_parent_identity=self._run_dir_identity,
                        )
                except Exception as rollback_exc:  # noqa: BLE001
                    return CheckpointResult(
                        status="failed",
                        reason=reason,
                        error=(
                            f"{write_error}; checkpoint patch rollback failed: "
                            f"{type(rollback_exc).__name__}: {rollback_exc}"
                        ),
                        preserved_previous_patch=False,
                        submission_eligible=False,
                    )
                return self._write_failure(reason=reason, error=write_error)
            preserved = False
        elif preserved:
            try:
                previous_patch = _read_bounded_text(
                    self.patch_path,
                    max_bytes=MAX_CHECKPOINT_PATCH_BYTES,
                    expected_parent_identity=self._run_dir_identity,
                )
                _unlink_durable(
                    self.patch_path,
                    expected_parent_identity=self._run_dir_identity,
                )
                preserved = False
            except (OSError, ValueError) as exc:
                return self._write_failure(
                    reason=reason,
                    error=(
                        "stale checkpoint patch removal failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
        stored_patch_sha = patch_sha
        meta = self._meta(
            "written" if patch.strip() else "empty",
            reason=reason,
            patch_bytes=patch_bytes,
            patch_sha256=stored_patch_sha,
            preserved_previous_patch=preserved and not patch.strip(),
            submission_eligible=bool(patch.strip()),
        )
        try:
            self._write_meta(meta)
        except Exception as exc:
            rollback_error = ""
            if patch.strip():
                try:
                    if previous_patch is None:
                        _unlink_durable(
                            self.patch_path,
                            expected_parent_identity=self._run_dir_identity,
                        )
                    else:
                        _atomic_write(
                            self.patch_path,
                            previous_patch,
                            expected_parent_identity=self._run_dir_identity,
                        )
                except Exception as rollback_exc:
                    rollback_error = (
                        f"; patch rollback failed: {type(rollback_exc).__name__}: "
                        f"{rollback_exc}"
                    )
            elif previous_patch is not None:
                try:
                    _atomic_write(
                        self.patch_path,
                        previous_patch,
                        expected_parent_identity=self._run_dir_identity,
                    )
                except Exception as rollback_exc:
                    rollback_error = (
                        "; stale patch rollback failed: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            return CheckpointResult(
                status="failed",
                reason=reason,
                error=(
                    f"checkpoint metadata write failed: {type(exc).__name__}: {exc}"
                    f"{rollback_error}"
                ),
                preserved_previous_patch=bool(previous_patch),
                submission_eligible=False,
            )
        return CheckpointResult(
            status=str(meta["status"]),
            patch_bytes=patch_bytes,
            patch_sha256=patch_sha,
            reason=reason,
            preserved_previous_patch=bool(meta["preserved_previous_patch"]),
            submission_eligible=bool(meta["submission_eligible"]),
        )

    async def restore_latest(
        self,
        env: Environment,
        *,
        exclude_paths: Sequence[str] = (),
    ) -> CheckpointResult:
        try:
            self._bind_run_directory()
        except Exception as exc:
            return CheckpointResult(
                status="failed",
                reason="restore",
                error=f"checkpoint run directory is unsafe: {type(exc).__name__}: {exc}",
                submission_eligible=False,
            )
        try:
            patch = _read_bounded_text(
                self.patch_path,
                max_bytes=MAX_CHECKPOINT_PATCH_BYTES,
                expected_parent_identity=self._run_dir_identity,
            )
        except FileNotFoundError:
            return CheckpointResult(status="missing", reason="restore")
        except (OSError, ValueError) as exc:
            return CheckpointResult(
                status="failed",
                reason="restore",
                error=f"checkpoint patch read failed: {type(exc).__name__}: {exc}",
                submission_eligible=False,
            )
        if not patch.strip():
            return CheckpointResult(status="empty", reason="restore")
        meta = self._load_meta()
        actual_patch_sha = _patch_sha(patch)
        patch_bytes = len(patch.encode("utf-8", errors="surrogatepass"))
        meta_error = _checkpoint_meta_integrity_error(
            meta,
            patch_bytes=patch_bytes,
            patch_sha256=actual_patch_sha,
        )
        if meta_error:
            return CheckpointResult(
                status="failed_metadata_integrity",
                patch_bytes=patch_bytes,
                patch_sha256=actual_patch_sha,
                reason="restore",
                error=meta_error,
                submission_eligible=False,
            )
        assert meta is not None
        if meta["submission_eligible"] is False:
            return CheckpointResult(
                status="skipped_not_submission_eligible",
                patch_bytes=patch_bytes,
                patch_sha256=actual_patch_sha,
                reason="restore",
                preserved_previous_patch=bool(meta.get("preserved_previous_patch")),
                submission_eligible=False,
            )
        restore_exclude_paths = (
            *exclude_paths,
            *self._artifact_exclude_paths(env),
        )
        precheck = await env.exec_cmd(
            worktree_diff_command(restore_exclude_paths),
            timeout=120,
        )
        truncation_error = _truncated_output_error(
            precheck,
            label="restore precheck diff",
        )
        if truncation_error:
            return CheckpointResult(
                status="failed",
                reason="restore",
                error=truncation_error,
                submission_eligible=False,
            )
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
        patch_sha = _patch_sha(patch)
        try:
            recovery_patch_path = await env.write_temp_file(
                patch,
                prefix="opencollab-checkpoint-recovery-",
                suffix=".patch",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # staging failed before Git could mutate
            return CheckpointResult(
                status="failed",
                patch_bytes=patch_bytes,
                patch_sha256=patch_sha,
                reason="restore",
                error=(
                    "checkpoint recovery patch staging failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                submission_eligible=False,
            )

        apply_result: ExecResult | None = None
        apply_error: BaseException | None = None
        try:
            apply_result = await env.exec_cmd(
                "git apply --binary --whitespace=nowarn "
                f"{shlex.quote(recovery_patch_path)}",
                timeout=120,
            )
        except BaseException as exc:
            apply_error = exc

        cleanup_failure, cancellation = await _remove_recovery_patch(
            env,
            recovery_patch_path,
            cancellation=(
                apply_error
                if isinstance(apply_error, asyncio.CancelledError)
                else None
            ),
            pending_tasks=self._owned_operations,
        )
        apply_succeeded = (
            apply_error is None
            and apply_result is not None
            and apply_result.returncode == 0
        )
        worktree_integrity_proven = True
        proof_error = ""
        if not apply_succeeded:
            (
                worktree_integrity_proven,
                proof_error,
                cancellation,
            ) = await _prove_failed_restore_clean(
                env,
                exclude_paths=restore_exclude_paths,
                cancellation=cancellation,
                pending_tasks=self._owned_operations,
            )
        if cancellation is not None:
            try:
                cancellation.checkpoint_restore_integrity_proven = (
                    worktree_integrity_proven
                )
            except (AttributeError, TypeError):
                pass
            add_note = getattr(cancellation, "add_note", None)
            if callable(add_note) and cleanup_failure is not None:
                add_note(
                    "checkpoint recovery temporary-file cleanup failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            if callable(add_note) and apply_error is not None and apply_error is not cancellation:
                add_note(
                    "checkpoint recovery apply also failed: "
                    f"{type(apply_error).__name__}: {apply_error}"
                )
            if callable(add_note) and proof_error:
                add_note(proof_error)
            raise cancellation
        if cleanup_failure is not None:
            error = (
                "checkpoint recovery temporary-file cleanup failed: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}"
            )
            if apply_error is not None:
                error += (
                    "; apply also failed: "
                    f"{type(apply_error).__name__}: {apply_error}"
                )
            if proof_error:
                error += f"; {proof_error}"
            return CheckpointResult(
                status="failed",
                patch_bytes=patch_bytes,
                patch_sha256=patch_sha,
                reason="restore",
                error=error,
                submission_eligible=False,
                worktree_integrity_proven=worktree_integrity_proven,
            )
        if apply_error is not None:
            if not isinstance(apply_error, Exception):
                try:
                    apply_error.checkpoint_restore_integrity_proven = (
                        worktree_integrity_proven
                    )
                except (AttributeError, TypeError):
                    pass
                raise apply_error
            error = (
                "checkpoint recovery apply failed: "
                f"{type(apply_error).__name__}: {apply_error}"
            )
            if proof_error:
                error += f"; {proof_error}"
            return CheckpointResult(
                status="failed",
                patch_bytes=patch_bytes,
                patch_sha256=patch_sha,
                reason="restore",
                error=error,
                submission_eligible=False,
                worktree_integrity_proven=worktree_integrity_proven,
            )

        assert apply_result is not None
        if apply_result.returncode == 0:
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
            error=(
                apply_result.stderr[:1000]
                + (f"; {proof_error}" if proof_error else "")
            ),
            worktree_integrity_proven=worktree_integrity_proven,
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
                worktree_integrity_proven=result.worktree_integrity_proven,
                background_errors=tuple(self._background_errors),
            )
        return result

    async def abort(
        self,
        *,
        timeout: float = DEFAULT_CHECKPOINT_ABORT_TIMEOUT,
    ) -> bool:
        """Stop periodic capture without reading a non-quiescent worktree.

        Returns ``True`` when the capture task has stopped.  A capture adapter
        that consumes cancellation cannot hold evaluator teardown forever: the
        task receives two cancellation requests and is then detached with its
        eventual result consumed.
        """
        try:
            phase_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("checkpoint abort timeout must be finite and positive") from exc
        if (
            isinstance(timeout, bool)
            or not math.isfinite(phase_timeout)
            or phase_timeout <= 0
        ):
            raise ValueError("checkpoint abort timeout must be finite and positive")

        self._stop.set()
        tasks = set(self._owned_operations)
        if self._task is not None:
            tasks.add(self._task)
            self._task = None
        if not tasks:
            return True

        for task in tasks:
            task.cancel()
        _done, pending_tasks = await asyncio.wait(tasks, timeout=phase_timeout)
        if pending_tasks:
            for task in pending_tasks:
                task.cancel()
            forced_timeout = min(
                MAX_FORCED_CHECKPOINT_ABORT_TIMEOUT,
                max(0.1, phase_timeout),
            )
            second_done, pending_tasks = await asyncio.wait(
                pending_tasks,
                timeout=forced_timeout,
            )
            _done.update(second_done)
        for task in _done:
            self._owned_operations.discard(task)
            self._consume_task_result(task)
        if pending_tasks:
            for task in pending_tasks:
                self._owned_operations.add(task)
                task.add_done_callback(self._consume_owned_task_result)
            return False
        return True

    def _consume_owned_task_result(self, task: asyncio.Future[Any]) -> None:
        self._owned_operations.discard(task)  # type: ignore[arg-type]
        self._consume_task_result(task)

    @staticmethod
    async def _wait_for_abort_task(
        task: asyncio.Task[Any], *, timeout: float
    ) -> bool:
        if task.done():
            return False
        _done, pending = await asyncio.wait({task}, timeout=timeout)
        return bool(pending)

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

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

    def _bind_run_directory(self) -> None:
        ensure_directory_no_symlinks(self.run_dir)
        run_dir_fd = _open_directory_no_symlinks(self.run_dir)
        try:
            run_dir_info = os.fstat(run_dir_fd)
            current = (run_dir_info.st_dev, run_dir_info.st_ino)
            if self._run_dir_identity is None:
                self._run_dir_identity = current
            elif current != self._run_dir_identity:
                raise OSError("checkpoint run directory identity changed")
        finally:
            os.close(run_dir_fd)

    def _has_existing_patch(self) -> bool:
        try:
            return bool(
                _read_bounded_text(
                    self.patch_path,
                    max_bytes=MAX_CHECKPOINT_PATCH_BYTES,
                    expected_parent_identity=self._run_dir_identity,
                ).strip()
            )
        except (OSError, ValueError):
            return False

    def _artifact_exclude_paths(self, env: Environment) -> tuple[str, ...]:
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
        for artifact_path in (self.patch_path, self.meta_path):
            try:
                lexical_artifact = Path(
                    os.path.abspath(os.fspath(artifact_path))
                )
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

    def _load_meta(self) -> dict[str, Any] | None:
        try:
            value = json.loads(
                _read_bounded_text(
                    self.meta_path,
                    max_bytes=MAX_CHECKPOINT_META_BYTES,
                    expected_parent_identity=self._run_dir_identity,
                )
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_meta(self, meta: dict[str, Any]) -> None:
        _atomic_write(
            self.meta_path,
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            expected_parent_identity=self._run_dir_identity,
        )

    def _write_failure(self, *, reason: str, error: str) -> CheckpointResult:
        preserved = self._has_existing_patch()
        preserved_patch = ""
        preserved_eligible = False
        if preserved:
            try:
                preserved_patch = _read_bounded_text(
                    self.patch_path,
                    max_bytes=MAX_CHECKPOINT_PATCH_BYTES,
                    expected_parent_identity=self._run_dir_identity,
                )
                previous_meta = self._load_meta()
                previous_sha = _patch_sha(preserved_patch)
                previous_meta_error = _checkpoint_meta_integrity_error(
                    previous_meta,
                    patch_bytes=len(
                        preserved_patch.encode(
                            "utf-8",
                            errors="surrogatepass",
                        )
                    ),
                    patch_sha256=previous_sha,
                )
                preserved_eligible = (
                    previous_meta_error is None
                    and previous_meta is not None
                    and previous_meta["submission_eligible"] is True
                )
            except (OSError, ValueError):
                preserved = False
                preserved_eligible = False
        meta = self._meta(
            "failed",
            reason=reason,
            patch_bytes=len(preserved_patch.encode("utf-8", errors="surrogatepass")),
            patch_sha256=_patch_sha(preserved_patch),
            error=error,
            preserved_previous_patch=preserved,
            submission_eligible=preserved_eligible,
        )
        try:
            self._write_meta(meta)
        except Exception as exc:
            error = (
                f"{error}; checkpoint metadata write failed: "
                f"{type(exc).__name__}: {exc}"
            )
        return CheckpointResult(
            status="failed",
            reason=reason,
            error=error,
            preserved_previous_patch=preserved,
            submission_eligible=preserved_eligible,
        )


async def _remove_recovery_patch(
    env: Environment,
    path: str,
    *,
    cancellation: asyncio.CancelledError | None,
    pending_tasks: set[asyncio.Task[Any]],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """Remove one restore-owned file within a fixed wall-clock bound."""
    try:
        cleanup_task = asyncio.create_task(env.remove_file(path))
    except BaseException as exc:
        return exc, cancellation
    pending_tasks.add(cleanup_task)

    cleanup_failure: BaseException | None = None
    deadline = asyncio.get_running_loop().time() + MAX_CHECKPOINT_TEMP_CLEANUP_SECONDS
    while not cleanup_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            cleanup_failure = TimeoutError(
                "checkpoint recovery temporary-file cleanup exceeded its deadline"
            )
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    WorktreeCheckpoint._consume_task_result(finished),
                )
            )
            break
        try:
            done, _pending = await asyncio.wait(
                {cleanup_task},
                timeout=remaining,
            )
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            continue
        if not done:
            cleanup_failure = TimeoutError(
                "checkpoint recovery temporary-file cleanup exceeded its deadline"
            )
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    WorktreeCheckpoint._consume_task_result(finished),
                )
            )
            break

    if cleanup_failure is None and cleanup_task.done():
        pending_tasks.discard(cleanup_task)
        try:
            cleanup_task.result()
        except BaseException as exc:
            cleanup_failure = exc
    return cleanup_failure, cancellation


async def _prove_failed_restore_clean(
    env: Environment,
    *,
    exclude_paths: Sequence[str],
    cancellation: asyncio.CancelledError | None,
    pending_tasks: set[asyncio.Task[Any]],
) -> tuple[bool, str, asyncio.CancelledError | None]:
    """Prove a failed/cancelled apply left no worktree mutation."""
    proof_task = asyncio.create_task(
        env.exec_cmd(worktree_diff_command(exclude_paths), timeout=120)
    )
    pending_tasks.add(proof_task)
    result: ExecResult | None = None
    proof_failure: BaseException | None = None
    deadline = asyncio.get_running_loop().time() + MAX_FAILED_RESTORE_PROOF_SECONDS
    while not proof_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            env._aborted = True
            proof_task.cancel()
            proof_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    WorktreeCheckpoint._consume_task_result(finished),
                )
            )
            return (
                False,
                "failed restore worktree proof exceeded its deadline",
                cancellation,
            )
        try:
            done, _pending = await asyncio.wait({proof_task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            continue
        if not done:
            continue

    pending_tasks.discard(proof_task)
    try:
        result = proof_task.result()
    except BaseException as exc:
        proof_failure = exc

    if proof_failure is not None:
        return (
            False,
            "failed restore worktree proof raised "
            f"{type(proof_failure).__name__}: {proof_failure}",
            cancellation,
        )
    if result is None:
        return False, "failed restore worktree proof produced no result", cancellation
    truncation_error = _truncated_output_error(
        result,
        label="failed restore worktree proof",
    )
    if truncation_error:
        return False, truncation_error, cancellation
    if result.returncode != 0:
        detail = result.stderr[:1000]
        return (
            False,
            "failed restore worktree proof exited "
            f"{result.returncode}" + (f": {detail}" if detail else ""),
            cancellation,
        )
    if result.stdout.strip():
        return (
            False,
            "failed checkpoint restore left the worktree dirty",
            cancellation,
        )
    return True, "", cancellation
