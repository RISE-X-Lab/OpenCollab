from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import Environment, ExecResult

ENV_RECOVERY_PATCH_PREFIX = "/tmp/opencollab-checkpoint-recovery-"


def _checkpoint_module():
    return sys.modules["opencollab.harness.swe_checkpoint"]


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
    deadline = asyncio.get_running_loop().time() + _checkpoint_module().MAX_CHECKPOINT_TEMP_CLEANUP_SECONDS
    while not cleanup_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            cleanup_failure = TimeoutError("checkpoint recovery temporary-file cleanup exceeded its deadline")
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    _checkpoint_module().WorktreeCheckpoint._consume_task_result(finished),
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
            cleanup_failure = TimeoutError("checkpoint recovery temporary-file cleanup exceeded its deadline")
            cleanup_task.cancel()
            cleanup_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    _checkpoint_module().WorktreeCheckpoint._consume_task_result(finished),
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
    try:
        retirement_collector = getattr(env, "registered_retirement_paths", None)
        registered_retirements = await retirement_collector() if callable(retirement_collector) else ()
    except BaseException as exc:
        return (
            False,
            f"failed restore retirement validation raised {type(exc).__name__}: {exc}",
            cancellation,
        )
    proof_task = asyncio.create_task(
        env.exec_cmd(
            _checkpoint_module().worktree_diff_command(
                exclude_paths,
                registered_retirement_paths=registered_retirements,
            ),
            timeout=120,
        )
    )
    pending_tasks.add(proof_task)
    result: ExecResult | None = None
    proof_failure: BaseException | None = None
    deadline = asyncio.get_running_loop().time() + _checkpoint_module().MAX_FAILED_RESTORE_PROOF_SECONDS
    while not proof_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            env._aborted = True
            proof_task.cancel()
            proof_task.add_done_callback(
                lambda finished: (
                    pending_tasks.discard(finished),
                    _checkpoint_module().WorktreeCheckpoint._consume_task_result(finished),
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
            f"failed restore worktree proof raised {type(proof_failure).__name__}: {proof_failure}",
            cancellation,
        )
    if result is None:
        return False, "failed restore worktree proof produced no result", cancellation
    truncation_error = _checkpoint_module()._truncated_output_error(
        result,
        label="failed restore worktree proof",
    )
    if truncation_error:
        return False, truncation_error, cancellation
    if result.returncode != 0:
        detail = result.stderr[:1000]
        return (
            False,
            f"failed restore worktree proof exited {result.returncode}" + (f": {detail}" if detail else ""),
            cancellation,
        )
    if result.stdout.strip():
        return (
            False,
            "failed checkpoint restore left the worktree dirty",
            cancellation,
        )
    if callable(retirement_collector):
        try:
            refreshed_snapshot = await retirement_collector()
        except BaseException as exc:
            return (
                False,
                f"failed restore retirement revalidation raised {type(exc).__name__}: {exc}",
                cancellation,
            )
        if tuple(refreshed_snapshot) != tuple(registered_retirements):
            return (
                False,
                "retirement artifacts changed during failed restore proof",
                cancellation,
            )
    return True, "", cancellation
