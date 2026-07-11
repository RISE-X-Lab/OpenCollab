"""Inject a benchmark test patch into the workspace before a workflow runs.

SWE-bench grades a candidate fix by applying its *own* ``test_patch`` (the real
FAIL_TO_PASS test) on top of the submitted ``model_patch``. To let the workflow
actually run that test while it works — instead of chasing a test that does not
exist yet at the base commit — the harness can apply that same ``test_patch``
into the live workspace up front. The injected test files are then checked out
right before ``model_patch`` extraction so they never leak into the submitted
diff (the grader would otherwise double-apply and conflict).

This module is benchmark plumbing: it lives in ``harness`` (outside
application/domain) and talks to the environment only through the
``EnvironmentPort`` surface.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from opencollab.application.async_timeout import force_task_terminal
from opencollab.application.exception_notes import add_exception_note

logger = logging.getLogger(__name__)

MAX_TEST_PATCH_BYTES = 8 * 1024 * 1024
MAX_TEST_PATCH_PATHS = 256
MAX_TEST_PATCH_PATH_BYTES = 512 * 1024
MAX_TEST_PATCH_ROLLBACK_SECONDS = 30.0
MAX_TEST_PATCH_ROLLBACK_COMMAND_SECONDS = 5.0
MAX_TEST_PATCH_TEMP_CLEANUP_SECONDS = 10.0
MAX_TEST_PATCH_FORCED_TASK_STOP_SECONDS = 0.1

_GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


class TestPatchIsolationError(RuntimeError):
    """Raised when a failed partial apply cannot be proven fully reverted."""

    __test__ = False

    def __init__(self, touched_paths: Sequence[str], detail: str) -> None:
        self.touched_paths = tuple(dict.fromkeys(str(path) for path in touched_paths))
        self.cancellation: asyncio.CancelledError | None = None
        super().__init__(detail)


def _decode_git_c_path(value: str) -> str:
    """Decode Git's double-quoted path syntax, including octal UTF-8 bytes."""
    if not value.startswith('"'):
        return value.split("\t", 1)[0].rstrip()

    decoded = bytearray()
    index = 1
    while index < len(value):
        char = value[index]
        if char == '"':
            break
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogatepass"))
            index += 1
            continue

        index += 1
        if index >= len(value):
            decoded.append(ord("\\"))
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(_GIT_C_ESCAPES.get(escaped, ord(escaped)))
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def _normalized_patch_path(value: str, *, strip_prefix: bool) -> str | None:
    path = _decode_git_c_path(value)
    if path == "/dev/null":
        return None
    if strip_prefix and (path.startswith("a/") or path.startswith("b/")):
        path = path[2:]
    return path or None


def _touched_files(patch: str) -> list[str]:
    """Parse pre/post-image and rename paths, including Git C-style quoting."""
    seen: dict[str, None] = {}
    prefixes = (
        ("--- ", True),
        ("+++ ", True),
        ("rename from ", False),
        ("rename to ", False),
        ("copy from ", False),
        ("copy to ", False),
    )
    for line in patch.splitlines():
        path = None
        for prefix, strip_prefix in prefixes:
            if line.startswith(prefix):
                try:
                    path = _normalized_patch_path(
                        line[len(prefix) :], strip_prefix=strip_prefix
                    )
                except (UnicodeError, ValueError):
                    path = None
                break
        if path:
            seen.setdefault(path, None)
    return list(seen)


def _numstat_paths(output: str) -> list[str]:
    """Read raw NUL-delimited paths from ``git apply --numstat -z``."""
    seen: dict[str, None] = {}
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:
            raise ValueError("malformed git apply --numstat -z output")
        path = fields[2]
        if path:
            seen.setdefault(path, None)
            continue
        if index + 1 >= len(records):
            raise ValueError("incomplete rename in git apply --numstat -z output")
        old_path = records[index]
        new_path = records[index + 1]
        index += 2
        if not old_path or not new_path:
            raise ValueError("empty rename path in git apply --numstat -z output")
        seen.setdefault(old_path, None)
        seen.setdefault(new_path, None)
    return list(seen)


def _path_bound_error(paths: Sequence[str]) -> str | None:
    if len(paths) > MAX_TEST_PATCH_PATHS:
        return f"path count {len(paths)} exceeds {MAX_TEST_PATCH_PATHS}"
    for path in paths:
        posix_path = PurePosixPath(path)
        components = path.split("/")
        if (
            not path
            or path.startswith("/")
            or PureWindowsPath(path).drive
            or "\\" in path
            or "\0" in path
            or any(component in {"", ".", ".."} for component in components)
            or any(part == ".." for part in posix_path.parts)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in path)
        ):
            return f"unsafe patch path: {path!r}"
    total_bytes = sum(
        len(path.encode("utf-8", errors="surrogatepass")) for path in paths
    )
    if total_bytes > MAX_TEST_PATCH_PATH_BYTES:
        return f"path bytes {total_bytes} exceed {MAX_TEST_PATCH_PATH_BYTES}"
    return None


async def apply_test_patch(env: Any, patch: str) -> list[str]:
    """Apply ``patch`` into ``env`` and return the test files it touched.

    Git apply is preflighted before it may mutate the worktree. A failed
    mutating command is rolled back and verified before returning. If clean
    state cannot be proven, ``TestPatchIsolationError`` carries every known
    path so the evaluator can stop before agent execution and exclude those
    paths from its final diff.
    """
    if not patch or not patch.strip():
        return []

    patch_bytes = len(patch.encode("utf-8", errors="surrogatepass"))
    if patch_bytes > MAX_TEST_PATCH_BYTES:
        logger.warning(
            "test injection: patch bytes %d exceed limit %d",
            patch_bytes,
            MAX_TEST_PATCH_BYTES,
        )
        return []

    touched = _touched_files(patch)
    bound_error = _path_bound_error(touched)
    if bound_error:
        logger.warning("test injection: refusing oversized path set: %s", bound_error)
        return []

    try:
        patch_path = await env.write_temp_file(
            patch,
            prefix="opencollab-test-patch-",
            suffix=".diff",
        )
    except Exception as exc:  # staging failed before any worktree mutation
        logger.warning("test injection: could not stage patch file: %s", exc)
        return []

    try:
        result = await _apply_staged_test_patch(env, patch_path, touched)
    except BaseException as original:
        await _finish_staged_patch_cleanup(
            env,
            patch_path,
            touched,
            original=original,
            mutation_applied=False,
        )
        raise AssertionError("staged patch cleanup did not preserve the exception")

    await _finish_staged_patch_cleanup(
        env,
        patch_path,
        result,
        original=None,
        mutation_applied=bool(result),
    )
    return result


async def _apply_staged_test_patch(
    env: Any,
    patch_path: str,
    touched: list[str],
) -> list[str]:
    """Apply one already-owned staging file; the caller always removes it."""
    quoted = shlex.quote(patch_path)
    try:
        numstat = await env.exec_cmd(f"git apply --numstat -z {quoted}")
    except Exception as exc:  # noqa: BLE001 - no mutation has happened yet
        logger.warning("test injection: numstat failed before apply: %s", exc)
        return []
    if (
        getattr(numstat, "returncode", 1) != 0
        or getattr(numstat, "stdout_truncated", False)
        or getattr(numstat, "stderr_truncated", False)
    ):
        logger.warning(
            "test injection: numstat was incomplete before apply "
            "(rc=%s, stdout_truncated=%s, stderr_truncated=%s)",
            getattr(numstat, "returncode", "?"),
            getattr(numstat, "stdout_truncated", False),
            getattr(numstat, "stderr_truncated", False),
        )
        return []
    try:
        numstat_paths = _numstat_paths(getattr(numstat, "stdout", "") or "")
    except ValueError as exc:
        logger.warning("test injection: numstat could not be parsed: %s", exc)
        return []
    touched = list(dict.fromkeys((*touched, *numstat_paths)))
    bound_error = _path_bound_error(touched)
    if bound_error:
        logger.warning("test injection: refusing oversized path set: %s", bound_error)
        return []
    check = await env.exec_cmd(f"git apply --check {quoted}")
    if getattr(check, "returncode", 1) == 0:
        try:
            result = await env.exec_cmd(f"git apply -v {quoted}")
        except BaseException as exc:
            logger.warning(
                "test injection: `git apply` raised after preflight: %s; "
                "rolling back touched paths",
                exc,
            )
            await _compensate_failed_mutation(env, touched, original=exc)
            return []
        if getattr(result, "returncode", 1) == 0:
            logger.info("test injection: applied patch touching %d file(s)", len(touched))
            return touched
        logger.warning(
            "test injection: `git apply` failed after a successful preflight "
            "(rc=%s): %s",
            getattr(result, "returncode", "?"),
            (getattr(result, "stderr", "") or "").strip()[:300],
        )
        await _compensate_failed_mutation(env, touched, original=None)
        return []

    logger.warning(
        "test injection: `git apply --check` failed (rc=%s): %s; "
        "continuing without injection",
        getattr(check, "returncode", "?"),
        (getattr(check, "stderr", "") or "").strip()[:300],
    )
    return []


async def _finish_staged_patch_cleanup(
    env: Any,
    patch_path: str,
    touched: Sequence[str],
    *,
    original: BaseException | None,
    mutation_applied: bool,
) -> None:
    """Remove a call-owned staging file while preserving failure provenance."""
    cancellation: asyncio.CancelledError | None = None
    if isinstance(original, asyncio.CancelledError):
        cancellation = original
    elif isinstance(original, TestPatchIsolationError):
        cancellation = original.cancellation

    cleanup_failure: BaseException | None = None
    cleanup_task: asyncio.Task[Any] | None = None
    try:
        cleanup_task = asyncio.create_task(env.remove_file(patch_path))
    except BaseException as exc:
        cleanup_failure = exc
    if cleanup_task is not None:
        deadline = (
            asyncio.get_running_loop().time()
            + MAX_TEST_PATCH_TEMP_CLEANUP_SECONDS
        )
        while not cleanup_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                cleanup_failure = TimeoutError(
                    "test patch staging-file cleanup exceeded its deadline"
                )
                env._aborted = True
                _stopped, cancellation, stop_error = await _force_stop_task(
                    cleanup_task,
                    cancellation=cancellation,
                )
                if stop_error:
                    add_exception_note(cleanup_failure, stop_error)
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
                    "test patch staging-file cleanup exceeded its deadline"
                )
                env._aborted = True
                _stopped, cancellation, stop_error = await _force_stop_task(
                    cleanup_task,
                    cancellation=cancellation,
                )
                if stop_error:
                    add_exception_note(cleanup_failure, stop_error)
                break

        if cleanup_failure is None and cleanup_task.done():
            try:
                cleanup_task.result()
            except BaseException as exc:
                cleanup_failure = exc

    known_paths = list(touched)
    if isinstance(original, TestPatchIsolationError):
        known_paths = list(
            dict.fromkeys((*original.touched_paths, *known_paths))
        )

    if cleanup_failure is not None:
        isolation_error = TestPatchIsolationError(
            known_paths,
            "test patch staging-file cleanup failed: "
            f"{type(cleanup_failure).__name__}: {cleanup_failure}",
        )
        isolation_error.cancellation = cancellation
        raise isolation_error from original

    if mutation_applied and cancellation is not None:
        isolation_error = TestPatchIsolationError(
            known_paths,
            "test patch staging cleanup was cancelled after mutation",
        )
        isolation_error.cancellation = cancellation
        raise isolation_error from original
    if isinstance(original, TestPatchIsolationError):
        raise original
    if cancellation is not None:
        raise cancellation
    if original is not None:
        raise original


async def _force_stop_task(
    task: asyncio.Task[Any],
    *,
    cancellation: asyncio.CancelledError | None,
) -> tuple[bool, asyncio.CancelledError | None, str]:
    """Finish a task that consumed ordinary cancellation after its deadline."""
    result = await force_task_terminal(
        task,
        timeout=MAX_TEST_PATCH_FORCED_TASK_STOP_SECONDS,
        cancellation=cancellation,
    )
    stop_notes = "; ".join(
        f"forced task termination reported {type(error).__name__}: {error}"
        for error in result.errors
    )
    return result.terminal, result.cancellation, stop_notes


async def _compensate_failed_mutation(
    env: Any,
    touched: list[str],
    *,
    original: BaseException | None,
) -> None:
    """Finish rollback despite repeated cancellation, then preserve cancellation."""
    cancellation = original if isinstance(original, asyncio.CancelledError) else None
    rollback_task = asyncio.create_task(_rollback_failed_apply(env, touched))
    rollback_failure: BaseException | None = None
    deadline = (
        asyncio.get_running_loop().time() + MAX_TEST_PATCH_ROLLBACK_SECONDS
    )
    while not rollback_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            env._aborted = True
            _stopped, cancellation, stop_error = await _force_stop_task(
                rollback_task,
                cancellation=cancellation,
            )
            rollback_failure = TestPatchIsolationError(
                touched,
                "failed test patch rollback exceeded its final deadline",
            )
            if stop_error:
                add_exception_note(rollback_failure, stop_error)
            break
        try:
            await asyncio.wait({rollback_task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            continue

    if rollback_failure is None and rollback_task.done():
        try:
            rollback_task.result()
        except BaseException as exc:
            rollback_failure = exc

    if rollback_failure is not None:
        if isinstance(rollback_failure, TestPatchIsolationError):
            isolation_error = rollback_failure
        else:
            isolation_error = TestPatchIsolationError(
                touched,
                "failed test patch rollback raised "
                f"{type(rollback_failure).__name__}: {rollback_failure}",
            )
        isolation_error.cancellation = cancellation
        raise isolation_error from original
    if cancellation is not None:
        raise cancellation
    if original is not None and not isinstance(original, Exception):
        raise original


async def _rollback_failed_apply(env: Any, touched: list[str]) -> None:
    """Restore partial-apply paths and prove their Git state is clean."""
    if not touched:
        raise TestPatchIsolationError(
            (),
            "failed apply reported no paths, so rollback scope is unknown",
        )

    failures: list[str] = []
    deadline_expired = False
    deadline = time.monotonic() + MAX_TEST_PATCH_ROLLBACK_SECONDS
    for path in touched:
        quoted_path = shlex.quote(path)
        commands = [
            f"git --literal-pathspecs checkout -- {quoted_path}",
            f"git --literal-pathspecs clean -fq -- {quoted_path}",
        ]
        command_failures: list[str] = []
        for command in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append(
                    "test patch rollback aggregate deadline expired before "
                    f"cleaning {path}"
                )
                deadline_expired = True
                break
            try:
                result = await env.exec_cmd(
                    command,
                    timeout=min(MAX_TEST_PATCH_ROLLBACK_COMMAND_SECONDS, remaining),
                )
            except Exception as exc:  # noqa: BLE001 - continue to state proof
                command_failures.append(f"{type(exc).__name__}: {exc}")
                continue
            if (
                getattr(result, "returncode", 1) != 0
                or getattr(result, "stdout_truncated", False)
                or getattr(result, "stderr_truncated", False)
            ):
                command_failures.append(
                    f"rc={getattr(result, 'returncode', '?')} for {command}"
                )

        if deadline_expired:
            break

        status_command = (
            "git --literal-pathspecs status --porcelain=v1 -z -- "
            + quoted_path
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append(
                "test patch rollback aggregate deadline expired before "
                f"verifying {path}"
            )
            break
        try:
            status = await env.exec_cmd(
                status_command,
                timeout=min(MAX_TEST_PATCH_ROLLBACK_COMMAND_SECONDS, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - converted to isolation error
            failures.append(
                f"{path}: status failed with {type(exc).__name__}: {exc}"
            )
            continue

        status_unknown = (
            getattr(status, "returncode", 1) != 0
            or getattr(status, "stdout_truncated", False)
            or getattr(status, "stderr_truncated", False)
        )
        dirty = bool((getattr(status, "stdout", "") or "").strip("\0\r\n "))
        if status_unknown or dirty:
            detail = (getattr(status, "stderr", "") or "").strip()
            if dirty:
                detail = (getattr(status, "stdout", "") or "").replace("\0", " | ").strip()
            if status_unknown and not detail:
                detail = (
                    f"status rc={getattr(status, 'returncode', '?')}, "
                    f"stdout_truncated={getattr(status, 'stdout_truncated', False)}, "
                    f"stderr_truncated={getattr(status, 'stderr_truncated', False)}"
                )
            if command_failures:
                detail = f"{detail}; cleanup: {'; '.join(command_failures)}"
            failures.append(f"{path}: {detail[:1000]}")

    if failures:
        raise TestPatchIsolationError(
            touched,
            "failed test patch rollback could not prove clean state: "
            + " | ".join(failures),
        )
