from __future__ import annotations

import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any


async def cleanup_injected_paths_and_extract_patch(
    facade: Any,
    *,
    env: Any,
    execution_quiesced: bool,
    injected_paths: Sequence[str],
    harness_artifact_paths: Sequence[str],
    cleanup_timeout: float,
    error: str | None,
    test_patch_isolation_failed: bool,
    harness_artifact_exclusion_proven: bool,
    checkpoint_restore_integrity_proven: bool,
    task_stage_integrity_proven: bool,
    await_teardown: Callable[[Awaitable[Any]], Awaitable[Any]],
) -> tuple[bool, str, bool, str | None]:
    """Remove injected tests, prove their cleanup, and capture a bounded diff."""
    cleanup_proven = not injected_paths
    if execution_quiesced and env and injected_paths:
        cleanup_proven = True
        cleanup_deadline = time.monotonic() + cleanup_timeout
        for index, path in enumerate(injected_paths):
            remaining_cleanup = cleanup_deadline - time.monotonic()
            if remaining_cleanup <= 0:
                cleanup_proven = False
                error = facade._append_harness_error(
                    error,
                    "test patch cleanup failed",
                    TimeoutError(
                        f"aggregate cleanup deadline expired with {len(injected_paths) - index} path(s) remaining"
                    ),
                )
                break
            quoted = shlex.quote(path)

            async def run_cleanup_command(command: str) -> Any:
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("aggregate injected-path cleanup deadline expired")
                return await await_teardown(env.exec_cmd(command, timeout=min(cleanup_timeout, remaining)))

            try:
                await run_cleanup_command(f"git --literal-pathspecs checkout -- {quoted}")
                await run_cleanup_command(f"git --literal-pathspecs clean -fq -- {quoted}")
                status = await run_cleanup_command(f"git --literal-pathspecs status --porcelain=v1 -z -- {quoted}")
                if (
                    status.returncode != 0
                    or status.stdout_truncated
                    or status.stderr_truncated
                    or status.stdout.strip()
                ):
                    detail = (status.stderr or status.stdout or "").strip()
                    if status.stdout_truncated or status.stderr_truncated:
                        detail = (
                            "status output truncated: "
                            f"stdout dropped {status.stdout_dropped_bytes} bytes, "
                            f"stderr dropped {status.stderr_dropped_bytes} bytes"
                        )
                    failure = RuntimeError(
                        f"injected path still dirty: {path}" + (f": {detail[:500]}" if detail else "")
                    )
                    error = facade._append_harness_error(error, "test patch cleanup failed", failure)
                    cleanup_proven = False
            except Exception as exc:
                error = facade._append_harness_error(error, "test patch cleanup failed", exc)
                cleanup_proven = False

    patch = ""
    extraction_succeeded = False
    if execution_quiesced and env:
        try:
            patch_result = await await_teardown(
                env.exec_cmd(facade.worktree_diff_command((*injected_paths, *harness_artifact_paths)))
            )
            patch = patch_result.stdout
            if patch_result.stdout_truncated or patch_result.stderr_truncated:
                patch = ""
                failure = RuntimeError(
                    "diff output truncated: "
                    f"stdout dropped {patch_result.stdout_dropped_bytes} bytes, "
                    f"stderr dropped {patch_result.stderr_dropped_bytes} bytes"
                )
                error = facade._append_harness_error(error, "patch extraction failed", failure)
            elif patch_result.returncode != 0:
                patch = ""
                detail = (patch_result.stderr or "").strip()
                failure = RuntimeError(
                    f"diff command exited {patch_result.returncode}" + (f": {detail[:500]}" if detail else "")
                )
                error = facade._append_harness_error(error, "patch extraction failed", failure)
            else:
                extraction_succeeded = True
                if (
                    test_patch_isolation_failed
                    or not harness_artifact_exclusion_proven
                    or not checkpoint_restore_integrity_proven
                    or not task_stage_integrity_proven
                ):
                    patch = ""
        except Exception as exc:
            error = facade._append_harness_error(error, "patch extraction failed", exc)
    return cleanup_proven, patch, extraction_succeeded, error
