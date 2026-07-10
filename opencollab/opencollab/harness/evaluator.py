"""Headless Evaluation Runner — for SWE-bench and research benchmarks.

Provides a pure, non-interactive entry point for batch evaluation.
Each task runs in an isolated environment, produces a git patch, and
records a full trajectory for analysis.

Ref:
- Design doc: run_eval_task with Environment + issue_desc → patch
- Harness Engineering: standardized output, sandboxed execution, trajectory recording
"""

from __future__ import annotations

import asyncio
import json
import math
import operator
import os
import shlex
import stat
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from opencollab.adapters.env import (
    DockerEnvironment,
    Environment,
    WorktreeEnvironment,
    _await_owned_operation,
)
from opencollab.adapters.repo_map import build_repo_map_via_env
from opencollab.adapters.safe_files import (
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
)
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.async_timeout import (
    CallerTimeoutError,
    abandon_on_timeout,
    force_task_terminal,
    isolate_tasks_from_shutdown,
)
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap import build_session
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent
from opencollab.harness.swe_checkpoint import WorktreeCheckpoint, worktree_diff_command
from opencollab.harness.test_injection import TestPatchIsolationError, apply_test_patch

EnvFactory = Callable[["EvalTask"], Awaitable[Environment]]
ToolFactory = Callable[[], Sequence[Tool]]


@dataclass
class EvalResult:
    """Result of a single evaluation task."""

    task_id: str
    patch: str  # Git diff output
    patch_produced: bool
    tokens_used: int
    steps: int
    duration: float
    error: str | None = None
    trajectory_path: str | None = None
    # Observability: count of LLM calls whose kimi tool-call markup was recovered
    # from literal text (P6), summed across every session of the run. Surfaces in
    # metrics.jsonl via asdict() — a regression alarm if it spikes (provider
    # quirk worsening) or drops to zero unexpectedly (recovery silently broke).
    markup_recovered: int = 0
    # Structured workflow return payload, when workflow mode is used. This is
    # observability plus a hook for outer SWE drivers that need workflow-level
    # audit data when writing the final prediction patch.
    workflow_result: Any | None = None
    checkpoint_result: Any | None = None
    # A failed partial test-patch rollback invalidates every later extraction
    # from the attached workspace, including extraction performed by an outer
    # SWE driver after this evaluator returns.
    test_patch_isolation_failed: bool = False
    execution_quiesced: bool = True
    patch_extraction_succeeded: bool = True
    injected_path_cleanup_proven: bool = True
    harness_artifact_exclusion_proven: bool = True
    checkpoint_restore_integrity_proven: bool = True
    task_stage_integrity_proven: bool = True
    submission_eligible: bool = True


@dataclass
class EvalTask:
    """A single evaluation task (e.g., one SWE-bench instance)."""

    task_id: str
    description: str  # Issue/problem description
    repo_path: str | None = None  # Path to repo (for local env)
    docker_image: str | None = None  # Docker image (for container env)
    timeout: float = 600.0  # Max seconds per task
    max_tokens: int = 1_000_000
    # Generic benchmark passthrough — never interpreted by the harness core, only
    # forwarded into the workflow args. SWE-bench uses it to thread the
    # ``test_patch`` (injected before the run) and parsed ``fail_to_pass`` ids.
    extras: dict | None = None
    # Host-side harness inputs that must stay outside checkpoint/final diffs.
    harness_artifact_paths: tuple[str, ...] = ()


EVAL_AGENT_PROMPT = """\
You are an autonomous coding agent. Complete the following task by modifying the code.

Rules:
- Read relevant files before making changes.
- Make minimal, targeted changes to fix the issue.
- After making changes, verify them (run tests if available).
- When done, make sure all changes are saved. Do NOT commit.
"""

DEFAULT_MAX_STEPS = 80
DEFAULT_EXECUTION_CLEANUP_TIMEOUT = 10.0
MAX_TASK_ID_BYTES = 240
RESULT_TEMP_DIRECTORY = ".opencollab-results-tmp"
MAX_LEGACY_RESULT_TEMP_ARTIFACTS = 256
MAX_RESULT_RECORD_BYTES = 64 * 1024 * 1024
MAX_RESULTS_FILE_BYTES = 512 * 1024 * 1024
MAX_TASK_HARNESS_ARTIFACT_PATHS = 256
MAX_TASK_HARNESS_ARTIFACT_PATH_BYTES = 32 * 1024
MAX_MAPPED_HARNESS_ARTIFACT_PATHS = 520
MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES = 128 * 1024
_EVAL_MANIFEST_OWNER_TASKS: set[asyncio.Task[Any]] = set()
_LATE_EVAL_RESOURCE_TASKS: set[asyncio.Task[Any]] = set()
_LATE_EVAL_RESOURCE_FAILURES: deque[BaseException] = deque(maxlen=64)


def _append_harness_error(current: str | None, stage: str, exc: Exception) -> str:
    detail = f"{stage}: {type(exc).__name__}: {exc}"
    return f"{current}; {detail}" if current else detail


def _validate_task_id(task_id: object) -> str:
    if not isinstance(task_id, str) or not task_id or task_id in {".", ".."}:
        raise ValueError("task_id must be a non-empty path-safe string")
    try:
        encoded_task_id = os.fsencode(task_id)
    except UnicodeEncodeError as exc:
        raise ValueError("task_id must be a non-empty path-safe string") from exc
    if (
        os.path.isabs(task_id)
        or PureWindowsPath(task_id).drive
        or "/" in task_id
        or "\\" in task_id
        or any(ord(char) < 32 or ord(char) == 127 for char in task_id)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in task_id)
        or len(encoded_task_id) > MAX_TASK_ID_BYTES
    ):
        raise ValueError("task_id must be a non-empty path-safe string")
    return task_id


def _task_id_collision_key(task_id: str) -> str:
    return unicodedata.normalize("NFC", task_id).casefold()


def _validate_harness_artifact_paths(paths: object) -> tuple[str, ...]:
    if not isinstance(paths, tuple):
        raise ValueError("harness_artifact_paths must be a tuple of non-empty strings")
    if len(paths) > MAX_TASK_HARNESS_ARTIFACT_PATHS:
        raise ValueError(
            "harness_artifact_paths exceeds the path-count safety bound"
        )
    normalized: list[str] = []
    total_bytes = 0
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or "\0" in path
            or any(0xD800 <= ord(char) <= 0xDFFF for char in path)
        ):
            raise ValueError(
                "harness_artifact_paths must contain filesystem-safe strings"
            )
        try:
            encoded = os.fsencode(path)
        except UnicodeEncodeError as exc:
            raise ValueError(
                "harness_artifact_paths must contain filesystem-safe strings"
            ) from exc
        total_bytes += len(encoded)
        if total_bytes > MAX_TASK_HARNESS_ARTIFACT_PATH_BYTES:
            raise ValueError(
                "harness_artifact_paths exceeds the aggregate-byte safety bound"
            )
        normalized.append(path)
    return tuple(normalized)


def _mapped_artifact_path_bound_error(paths: Sequence[str]) -> str | None:
    if len(paths) > MAX_MAPPED_HARNESS_ARTIFACT_PATHS:
        return (
            f"mapped artifact path count {len(paths)} exceeds "
            f"{MAX_MAPPED_HARNESS_ARTIFACT_PATHS}"
        )
    total_bytes = sum(
        len(path.encode("utf-8", errors="surrogatepass")) for path in paths
    )
    if total_bytes > MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES:
        return (
            f"mapped artifact path bytes {total_bytes} exceed "
            f"{MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES}"
        )
    return None


def _host_workspace_root(env: Environment) -> Path | None:
    raw_workspace = (
        env.workspace
        if env.local_filesystem
        else getattr(env, "host_workspace", None)
    )
    if not raw_workspace:
        return None
    try:
        return Path(os.path.abspath(os.fspath(raw_workspace)))
    except (OSError, TypeError, ValueError):
        return None


def _workspace_relative_host_paths(
    env: Environment,
    raw_path: str | os.PathLike[str],
) -> list[Path]:
    workspace = _host_workspace_root(env)
    if workspace is None:
        return []
    relative_paths: list[Path] = []
    try:
        target = Path(os.path.abspath(os.fspath(raw_path)))
    except (OSError, TypeError, ValueError):
        return []
    roots = [workspace]
    raw_source_workspace = getattr(env, "source_workspace", None)
    if raw_source_workspace:
        try:
            source_workspace = Path(
                os.path.abspath(os.fspath(raw_source_workspace))
            )
        except (OSError, TypeError, ValueError):
            source_workspace = None
        if source_workspace is not None and source_workspace not in roots:
            roots.append(source_workspace)
    for root in roots:
        pairs = [(target, root)]
        try:
            pairs.append(
                (target.resolve(strict=False), root.resolve(strict=False))
            )
        except (OSError, RuntimeError):
            pass
        for candidate, candidate_root in pairs:
            try:
                relative = candidate.relative_to(candidate_root)
            except ValueError:
                continue
            if relative not in relative_paths:
                relative_paths.append(relative)
    return relative_paths


def _workspace_relative_host_path(
    env: Environment,
    raw_path: str | os.PathLike[str],
) -> Path | None:
    paths = _workspace_relative_host_paths(env, raw_path)
    return paths[0] if paths else None


def _workspace_relative_artifact_paths(
    env: Environment,
    paths: Sequence[str | os.PathLike[str]],
) -> list[str]:
    relative_paths: list[Path] = []
    for raw_path in paths:
        for relative in _workspace_relative_host_paths(env, raw_path):
            if relative == Path("."):
                continue
            if relative not in relative_paths:
                relative_paths.append(relative)

    selected: list[Path] = []
    for relative in sorted(relative_paths, key=lambda path: len(path.parts)):
        if any(parent == relative or parent in relative.parents for parent in selected):
            continue
        selected.append(relative)
    return [path.as_posix() for path in selected]


def _legacy_result_temp_paths(output_dir: str) -> tuple[list[str], bool]:
    matches: list[str] = []
    try:
        with os.scandir(output_dir) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(".results.jsonl.")
                    and entry.name.endswith(".tmp")
                ):
                    continue
                if len(matches) >= MAX_LEGACY_RESULT_TEMP_ARTIFACTS:
                    return matches, False
                matches.append(entry.path)
    except OSError:
        return matches, False
    return matches, True


def default_tools() -> list[Tool]:
    """Build the default tool set for an eval agent.

    Mirrors the curated single-agent surface used by team roles (coder +
    reviewer tools) so headless eval exercises the same toolset: the bash
    description deflects to run_tests/git_diff/grep, and apply_patch is the
    fallback when str_replace edits fail to match.
    """
    return [
        BashTool(require_process_isolation=True),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        RunTestsTool(
            allow_runner_override=False,
            allow_extra_args=False,
            require_process_isolation=True,
        ),
        GitDiffTool(),
        GrepTool(),
    ]


async def default_env_factory(task: EvalTask) -> Environment:
    """Build the default environment for a task (docker if imaged, else local)."""
    if task.docker_image:
        backing: WorktreeEnvironment | None = None
        if task.repo_path:
            backing = WorktreeEnvironment(source_workspace=task.repo_path)
        env = DockerEnvironment(image=task.docker_image, backing_environment=backing)
        try:
            mount_dir = None
            if backing is not None:
                mount_dir = await backing.setup()
            await env.setup(mount_dir=mount_dir)
            return env
        except BaseException as original:
            try:
                await _await_owned_operation(env.cleanup())
            except BaseException as cleanup_exc:
                add_note = getattr(original, "add_note", None)
                if callable(add_note):
                    add_note(
                        "isolated Docker environment cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            raise original
    env = WorktreeEnvironment(source_workspace=task.repo_path or ".")
    await env.setup()
    return env


class _EvalSessionFactory:
    """``WorkflowSessionFactoryPort`` bound to one eval task's shared env.

    Every ``build_workflow_session`` call assembles a fresh one-shot ``Agent`` on
    the *same* task ``Environment`` (so each workflow agent sees the cumulative
    working-tree changes and the final ``git diff`` aggregates them) and the same
    tracer. The caller's ``tools`` override the default eval toolset when given.

    When ``save_dir`` is set, each session's conversation is autosaved per role
    (``<seq>_<role>.json``) into the task's run folder — mirroring the team /
    CLI-workflow layout so an eval workflow reads as its roles, not one flat
    trajectory. ``None`` keeps sessions ephemeral (the prior behaviour).
    """

    def __init__(
        self,
        *,
        env: Environment,
        tracer: Tracer,
        prompt: str,
        model: str,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        max_steps: int,
        default_toolset: Sequence[Tool],
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float | None = DEFAULT_TOP_P,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        save_dir: str | None = None,
    ) -> None:
        self._env = env
        self._tracer = tracer
        self._prompt = prompt
        self._model = model
        self._provider = provider
        self._api_key = api_key
        self._base_url = base_url
        self._max_steps = max_steps
        self._default_toolset = list(default_toolset)
        self._temperature = temperature
        self._top_p = top_p
        self._thinking = thinking
        self._thinking_params = (
            thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        )
        self._save_dir = save_dir
        self._session_seq = 0

    def _next_save_path(self, label: str | None) -> str | None:
        """Per-session transcript path within the task run folder, or ``None``.

        ``<save_dir>/<seq>_<role>.json``. The sequence counter orders sessions
        by creation and disambiguates a role that runs more than once; bumping
        it has no ``await`` so it is atomic under cooperative scheduling even
        when ``parallel``/``pipeline`` build many sessions concurrently.
        """
        if self._save_dir is None:
            return None
        seq = self._session_seq
        self._session_seq += 1
        return workflow_transcript_path(self._save_dir, seq, label)

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> Session:
        # ``thinking`` None -> run-wide default; an explicit value (False for the
        # schema-only structured agents) overrides it to shorten their slow
        # reasoning generations.
        use_thinking = self._thinking if thinking is None else thinking
        agent = Agent(
            name="eval_agent",
            system_prompt=self._prompt,
            tools=list(tools) if tools is not None else list(self._default_toolset),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            top_p=self._top_p,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        return build_session(
            agent=agent,
            env=self._env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            max_steps=self._max_steps,
            auto_save_path=self._next_save_path(label),
        )


def _build_eval_session_factory(
    *,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    default_toolset: Sequence[Tool],
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    save_dir: str | None = None,
) -> _EvalSessionFactory:
    """Construct the per-task workflow session factory (seam for tests)."""
    return _EvalSessionFactory(
        env=env,
        tracer=tracer,
        prompt=prompt,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        default_toolset=default_toolset,
        temperature=temperature,
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
        save_dir=save_dir,
    )


async def _run_single_session(
    *,
    task: EvalTask,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    tools: Sequence[Tool],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    session_holder: list[Session] | None = None,
    owned_tasks: list[asyncio.Task[Any]] | None = None,
) -> Session:
    """Drive the unchanged single-session eval loop and return the session."""
    agent = Agent(
        name="eval_agent",
        system_prompt=prompt,
        tools=list(tools),
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS),
    )
    session = build_session(
        agent=agent,
        env=env,
        tracer=tracer,
        max_budget_tokens=task.max_tokens,
        max_steps=max_steps,
    )
    if session_holder is not None:
        session_holder.append(session)
    deadline = time.monotonic() + task.timeout
    add_message_task = asyncio.create_task(session.add_user_message(task.description))
    if owned_tasks is not None:
        owned_tasks.append(add_message_task)
    await abandon_on_timeout(add_message_task, task.timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CallerTimeoutError
    run_task = asyncio.create_task(session.run_loop())
    if owned_tasks is not None:
        owned_tasks.append(run_task)
    await abandon_on_timeout(run_task, remaining)
    return session


async def _run_workflow_mode(
    *,
    task: EvalTask,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    tools: Sequence[Tool],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    workflow: WorkflowFn,
    injected_paths: Sequence[str] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    save_dir: str | None = None,
    context_holder: list[WorkflowContext] | None = None,
    owned_tasks: list[asyncio.Task[Any]] | None = None,
    timeout_error_seconds: float | None = None,
) -> WorkflowContext:
    """Run ``workflow`` over a task-bound context; return the context.

    The context's session factory is bound to the shared task env, so each
    workflow agent sees cumulative changes and the final ``git diff`` aggregates
    them. The shared env is attached as ``ctx.env`` (a harness convention) so
    harness-layer workflows can read the working-tree diff. ``tokens_used`` /
    ``steps`` are aggregated by the caller across every session created.

    ``save_dir`` (the task's run folder) is threaded to the factory so each
    session's conversation is autosaved per role (``<seq>_<role>.json``).
    """
    factory = _build_eval_session_factory(
        env=env,
        tracer=tracer,
        prompt=prompt,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        default_toolset=tools,
        temperature=temperature,
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
        save_dir=save_dir,
    )
    # Wall-clock deadline on the monotonic clock: the workflow checks
    # ``ctx.time_low()`` and bails to a forced final write before the
    # the caller deadline below truncates the run (P7). ``task.timeout`` is
    # the hard wall (1800s for analyst-solve); the workflow leaves itself
    # ``DEFAULT_DEADLINE_MARGIN_SECONDS`` of head-room inside it.
    deadline = time.monotonic() + task.timeout
    ctx = WorkflowContext(
        factory,
        tracer=tracer,
        budget_total=task.max_tokens,
        tree_probe=EnvWorkingTreeProbe(env),
        deadline_monotonic=deadline,
    )
    if context_holder is not None:
        context_holder.append(ctx)
    ctx.env = env  # type: ignore[attr-defined] — harness seam for workflows
    args = dict(task.extras or {})
    args.update({"task_id": task.task_id, "description": task.description})
    # Forward benchmark passthrough (e.g. SWE-bench fail_to_pass ids + the paths
    # of any injected test files) so the workflow can scope to the target tests.
    args.pop("injected_test_paths", None)
    # Preserve every declared FAIL_TO_PASS id even when no test patch was
    # supplied or injection produced no paths. The workflow must execute the
    # exact targets before it may report PASS; unavailable targets therefore
    # remain a technical failure instead of silently bypassing the hard gate.
    if injected_paths:
        args["injected_test_paths"] = list(injected_paths)
    # ALWAYS return the ctx, even when the workflow ends abnormally. The ctx is
    # already fully built (above) and its ``.sessions`` accumulate token+step
    # metrics as agents run, so by the time the body raises it holds the real
    # cost of the run AND a partial patch sits on disk. Letting the exception
    # propagate to the caller would leave ``workflow_ctx`` None there and zero out
    # both — the regression that lost django-11564 (an outer-wall timeout) and the
    # sympy budget-floor runs. Catch the controlled-stop cases here and return ctx.
    workflow_task = asyncio.create_task(workflow(ctx, args))
    if owned_tasks is not None:
        owned_tasks.append(workflow_task)
    try:
        ctx.workflow_result = await abandon_on_timeout(workflow_task, task.timeout)  # type: ignore[attr-defined]
    except WorkflowBudgetExceeded as exc:
        # Budget floor stopping the run is BY DESIGN, not a failure: prior coder
        # rounds / the forced final write have already written a real patch, and
        # ctx holds every session's metrics. Not surfaced as an error.
        await ctx.log(f"workflow stopped at budget floor — {exc}")
    except CallerTimeoutError:
        reported_timeout = (
            task.timeout if timeout_error_seconds is None else timeout_error_seconds
        )
        ctx.workflow_error = f"Task timed out after {reported_timeout}s"  # type: ignore[attr-defined]
        await ctx.log(f"workflow ended early — {ctx.workflow_error}")
    except Exception as exc:  # noqa: BLE001 — the harness must never lose a run
        # Provider/session failures keep the ctx so
        # the partial on-disk patch + accumulated metrics survive. Record the cause
        # for observability; patch_produced stays honest off the real on-disk diff.
        ctx.workflow_error = f"{type(exc).__name__}: {exc}"  # type: ignore[attr-defined]
        await ctx.log(f"workflow ended early — {ctx.workflow_error}")
    return ctx


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "used_tokens", 0)) for s in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "step_count", 0)) for s in sessions)


def _aggregate_markup_recovery(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "markup_recovered", 0)) for s in sessions)


async def _wait_for_owned_execution(
    tasks: Sequence[asyncio.Task[Any]],
    workflow_ctx: WorkflowContext | None,
    *,
    cleanup_timeout: float,
) -> bool:
    """Bound cancellation cleanup before diff extraction and env teardown."""

    def pending_tasks() -> set[asyncio.Task[Any]]:
        pending = {task for task in tasks if not task.done()}
        if workflow_ctx is not None:
            pending.update(workflow_ctx.pending_cleanup_tasks)
        return pending

    async def wait_phase(timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        saw_empty = False
        while True:
            pending = pending_tasks()
            if not pending:
                if saw_empty:
                    return True
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _done, still_pending = await asyncio.wait(pending, timeout=remaining)
            if still_pending:
                return False

    if await wait_phase(cleanup_timeout):
        return True

    # A coroutine may consume the first CancelledError while unwinding. A
    # second cancellation interrupts that cleanup wait without making the
    # ordinary deadline depend on an unbounded provider/tool finally block.
    pending = pending_tasks()
    for task in pending:
        task.cancel()
    forced_timeout = min(2.0, max(0.1, cleanup_timeout))
    if await wait_phase(forced_timeout):
        return True
    await isolate_tasks_from_shutdown(
        pending_tasks(),
        timeout=forced_timeout,
    )
    # Non-terminal results were removed from loop-shutdown ownership. The false
    # return keeps submission eligibility conservative while teardown continues.
    return False


async def _abort_environment(
    env: Environment,
    *,
    cleanup_timeout: float,
) -> bool:
    # Revoke the public environment surface synchronously, before an adapter's
    # resource-specific abort hook gets a chance to block or consume cancel.
    env._aborted = True
    abort = getattr(env, "abort", None)
    if not callable(abort):
        return True
    task = asyncio.ensure_future(abort())
    if await _wait_for_owned_execution([task], None, cleanup_timeout=cleanup_timeout):
        if task.cancelled():
            return False
        task.result()
        return True
    task.add_done_callback(lambda finished: _consume_background_task(finished))
    return False


async def _cleanup_environment_bounded(
    env: Environment,
    *,
    cleanup_timeout: float,
) -> bool:
    cleanup_task = asyncio.ensure_future(env.cleanup())
    quiesced = await _wait_for_owned_execution(
        [cleanup_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if cleanup_task.cancelled():
            return False
        cleanup_task.result()
    if not quiesced:
        cleanup_task.add_done_callback(_consume_background_task)
    return quiesced


class _EnvironmentSetupOwner:
    """Keep setup and any late environment under one task's ownership.

    The owner task stays alive until ``run_eval_task`` either accepts the
    environment or relinquishes it.  A factory that consumes cancellation and
    returns after the caller-side deadline is therefore disposed *inside the
    already-owned setup task*.  This matters during ``asyncio.run`` shutdown:
    the runner waits for the original task, while a cleanup task created by a
    done callback would be absent from the runner's cancellation snapshot and
    could be destroyed when the loop closes.

    Each teardown operation runs in a shielded child with a fixed deadline.
    Cancellation aimed at the owner therefore cannot skip disposal, while an
    adapter that never completes becomes explicit disposal evidence instead of
    keeping the event loop alive indefinitely.
    """

    def __init__(
        self,
        factory: EnvFactory,
        eval_task: EvalTask,
        *,
        cleanup_timeout: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._factory = factory
        self._eval_task = eval_task
        self._cleanup_timeout = cleanup_timeout
        self._delivery: asyncio.Future[Environment] = loop.create_future()
        self._decision = asyncio.Event()
        self._transferred = False
        self._dispose_requested = False
        self._environment: Environment | None = None
        self.disposal_errors: list[tuple[str, BaseException]] = []
        self.task = loop.create_task(self._run())
        self.task.add_done_callback(_consume_background_task)

    async def acquire(self, timeout: float) -> Environment:
        return await abandon_on_timeout(self._delivery, timeout)

    def transfer(self, env: Environment) -> None:
        if env is not self._environment:
            raise RuntimeError("environment setup ownership transfer mismatch")
        if self._dispose_requested:
            raise RuntimeError("environment setup ownership was already relinquished")
        self._transferred = True
        self._decision.set()

    def relinquish(self) -> None:
        self._dispose_requested = True
        self._decision.set()
        if not self.task.done():
            self.task.cancel()

    async def _finish_teardown_operation(
        self,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            operation_task = asyncio.ensure_future(operation())
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))
            return

        waiter = asyncio.create_task(
            asyncio.wait(
                {operation_task},
                timeout=self._cleanup_timeout,
            )
        )
        while True:
            try:
                _done, pending = await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    _done, pending = waiter.result()
                    break
                continue
        if pending:
            operation_task.cancel()
            termination = await force_task_terminal(
                operation_task,
                timeout=self._cleanup_timeout,
            )
            for termination_error in termination.errors:
                self.disposal_errors.append((stage, termination_error))
            self.disposal_errors.append(
                (
                    stage,
                    TimeoutError(
                        f"teardown operation exceeded {self._cleanup_timeout}s"
                    ),
                )
            )
            return
        try:
            operation_task.result()
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))

    async def _dispose(self, env: Environment) -> None:
        # Revoke the adapter synchronously before any teardown await.  A late
        # factory cannot hand a still-active environment to another consumer.
        env._aborted = True
        await self._finish_teardown_operation("environment abort failed", env.abort)
        await self._finish_teardown_operation(
            "environment cleanup failed",
            env.cleanup,
        )

    async def _run(self) -> None:
        try:
            env = await self._factory(self._eval_task)
        except asyncio.CancelledError:
            # A factory that propagates cancellation created no transferable
            # environment.  Factories that consume it continue below and their
            # eventual result remains owned here.
            return
        except BaseException as exc:
            if not self._delivery.done():
                self._delivery.set_exception(exc)
            return

        if not isinstance(env, Environment):
            if not self._delivery.done():
                self._delivery.set_exception(
                    TypeError("env_factory must return an Environment instance")
                )
            return

        self._environment = env
        if self._delivery.done():
            self._dispose_requested = True
        else:
            self._delivery.set_result(env)

        while not self._transferred and not self._dispose_requested:
            try:
                await self._decision.wait()
            except asyncio.CancelledError:
                self._dispose_requested = True

        if self._transferred and not self._dispose_requested:
            return
        await self._dispose(env)


async def _stop_checkpoint_bounded(
    checkpoint: WorktreeCheckpoint,
    env: Environment,
    *,
    exclude_paths: Sequence[str],
    cleanup_timeout: float,
) -> tuple[bool, Any | None]:
    stop_task = asyncio.ensure_future(
        checkpoint.stop(env, exclude_paths=exclude_paths)
    )
    quiesced = await _wait_for_owned_execution(
        [stop_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if stop_task.cancelled():
            return False, None
        return True, stop_task.result()
    stop_task.add_done_callback(_consume_background_task)
    return False, None


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _workflow_persistence_errors(ctx: WorkflowContext) -> tuple[Exception, ...]:
    errors: list[Exception] = []
    for session in ctx.sessions:
        for error in getattr(session, "persistence_errors", ()):
            if isinstance(error, Exception):
                errors.append(error)
    return tuple(errors)


async def _finalize_eval_workflow_sessions(
    ctx: WorkflowContext,
    *,
    cleanup_timeout: float,
) -> tuple[bool, tuple[Exception, ...], tuple[asyncio.Task[Any], ...]]:
    """Freeze final session states after execution and await their writers."""
    enqueue_errors: list[Exception] = []
    for session in ctx.sessions:
        enqueue = getattr(session, "enqueue_auto_save", None)
        if not callable(enqueue):
            continue
        try:
            enqueue()
        except Exception as exc:
            enqueue_errors.append(exc)
    quiesced = await _wait_for_owned_execution(
        [],
        ctx,
        cleanup_timeout=cleanup_timeout,
    )
    pending = tuple(
        task
        for task in ctx.pending_cleanup_tasks
        if isinstance(task, asyncio.Task) and not task.done()
    )
    return (
        quiesced,
        (*enqueue_errors, *_workflow_persistence_errors(ctx)),
        pending,
    )


def _eval_manifest_payload(
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
) -> dict[str, Any]:
    """Freeze values owned by the event-loop before file I/O starts."""
    return {
        "workflow": getattr(workflow, "__name__", "workflow"),
        "task_id": task.task_id,
        "sessions": len(ctx.sessions),
        "tokens_spent": ctx.budget.spent(),
        "budget_total": ctx.budget.total,
    }


def _write_eval_workflow_manifest(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Write ``<run_dir>/workflow.json`` summarising an eval workflow run."""
    if manifest is None:
        manifest = _eval_manifest_payload(
            task=task,
            workflow=workflow,
            ctx=ctx,
        )
    SessionStore().save_manifest(
        os.path.join(run_dir, WORKFLOW_MANIFEST_FILENAME), manifest
    )


def _eval_manifest_owner_done(task: asyncio.Task[Any]) -> None:
    _EVAL_MANIFEST_OWNER_TASKS.discard(task)
    _consume_background_task(task)


async def _persist_eval_workflow_manifest_owned(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
    cleanup_timeout: float,
) -> tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]:
    manifest = _eval_manifest_payload(task=task, workflow=workflow, ctx=ctx)
    subscriber = AutoSaveSubscriber(
        lambda: _write_eval_workflow_manifest(
            run_dir,
            task=task,
            workflow=workflow,
            ctx=ctx,
            manifest=manifest,
        )
    )
    owner = subscriber.enqueue()
    if owner is None:
        return True, subscriber.last_error, ()
    _EVAL_MANIFEST_OWNER_TASKS.add(owner)
    owner.add_done_callback(_eval_manifest_owner_done)
    pending: set[asyncio.Task[Any]] = {owner}
    _done, pending = await asyncio.wait(pending, timeout=cleanup_timeout)
    if pending:
        for pending_task in pending:
            pending_task.cancel()
        _done, pending = await asyncio.wait(pending, timeout=cleanup_timeout)
    if pending:
        await isolate_tasks_from_shutdown(pending, timeout=cleanup_timeout)
    return not pending, subscriber.last_error, tuple(pending)


async def _cleanup_eval_resources_after_tasks(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Tracer,
    timeout: float,
) -> None:
    quiesced = False
    try:
        pending = {task for task in dependencies if not task.done()}
        deadline = asyncio.get_running_loop().time() + timeout
        while pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            waiter = asyncio.create_task(
                asyncio.wait(pending, timeout=remaining)
            )
            while True:
                try:
                    await asyncio.shield(waiter)
                    break
                except asyncio.CancelledError:
                    if waiter.done():
                        break
                    continue
            _done, pending = waiter.result()
        quiesced = not pending
        if pending:
            await isolate_tasks_from_shutdown(pending, timeout=timeout)
    finally:
        if not quiesced:
            _LATE_EVAL_RESOURCE_FAILURES.append(
                TimeoutError(
                    "late evaluator tracer dependencies did not quiesce before "
                    "their final deadline"
                )
            )
        try:
            tracer.close()
        except BaseException as exc:
            _LATE_EVAL_RESOURCE_FAILURES.append(exc)
            raise


def _late_eval_resource_done(task: asyncio.Task[Any]) -> None:
    _LATE_EVAL_RESOURCE_TASKS.discard(task)
    _consume_background_task(task)


def _defer_eval_resource_cleanup(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Tracer,
    timeout: float,
) -> None:
    late_timeout = min(2.0, max(0.1, timeout))
    owner = asyncio.create_task(
        _cleanup_eval_resources_after_tasks(
            dependencies,
            tracer=tracer,
            timeout=late_timeout,
        )
    )
    _LATE_EVAL_RESOURCE_TASKS.add(owner)
    owner.add_done_callback(_late_eval_resource_done)


async def run_eval_task(
    task: EvalTask,
    model: str = "gpt-4o",
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    output_dir: str = "eval_results",
    prompt: str = EVAL_AGENT_PROMPT,
    tools_factory: ToolFactory = default_tools,
    env_factory: EnvFactory = default_env_factory,
    max_steps: int = DEFAULT_MAX_STEPS,
    workflow: WorkflowFn | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    checkpoint_interval_seconds: float | None = None,
    resume_from_checkpoint: bool = False,
    cancellation_cleanup_timeout: float = DEFAULT_EXECUTION_CLEANUP_TIMEOUT,
) -> EvalResult:
    """Run a single evaluation task.

    With ``workflow=None`` (default) this drives one agent session in the task's
    isolated default workspace. When a ``workflow`` is given, the task is
    orchestrated by that workflow function over a ``WorkflowContext`` whose
    session factory is bound to the task env / budget. ``tokens_used`` and
    ``steps`` then aggregate across *all* sessions the workflow created (sum
    semantics — a workflow's cost is the cost of every agent it ran). Patch
    extraction, timeout handling, and the ``EvalResult`` shape are shared by
    both modes.
    """
    task_id = _validate_task_id(task.task_id)
    if not isinstance(task.description, str):
        raise ValueError("task description must be a string")
    if isinstance(task.max_tokens, bool):
        raise ValueError("task max_tokens must be a positive integer")
    try:
        task_max_tokens = operator.index(task.max_tokens)
    except TypeError as exc:
        raise ValueError("task max_tokens must be a positive integer") from exc
    if task_max_tokens <= 0:
        raise ValueError("task max_tokens must be a positive integer")
    if isinstance(max_steps, bool):
        raise ValueError("max_steps must be a positive integer")
    try:
        normalized_max_steps = operator.index(max_steps)
    except TypeError as exc:
        raise ValueError("max_steps must be a positive integer") from exc
    if normalized_max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if task.extras is not None and not isinstance(task.extras, dict):
        raise ValueError("task extras must be a dictionary or None")
    if (
        isinstance(task.extras, dict)
        and "test_patch" in task.extras
        and not isinstance(task.extras["test_patch"], str)
    ):
        raise ValueError("task extras test_patch must be a string")
    harness_artifact_inputs = _validate_harness_artifact_paths(
        task.harness_artifact_paths
    )
    try:
        task_timeout = float(task.timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("task timeout must be a finite positive number") from exc
    if isinstance(task.timeout, bool) or not math.isfinite(task_timeout) or task_timeout <= 0:
        raise ValueError("task timeout must be a finite positive number")

    normalized_checkpoint_interval: float | None = None
    if checkpoint_interval_seconds is not None:
        try:
            normalized_checkpoint_interval = float(checkpoint_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "checkpoint_interval_seconds must be finite and non-negative"
            ) from exc
        if (
            isinstance(checkpoint_interval_seconds, bool)
            or not math.isfinite(normalized_checkpoint_interval)
            or normalized_checkpoint_interval < 0
        ):
            raise ValueError(
                "checkpoint_interval_seconds must be finite and non-negative"
            )
        if normalized_checkpoint_interval == 0:
            normalized_checkpoint_interval = None

    try:
        cleanup_timeout = float(cancellation_cleanup_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "cancellation_cleanup_timeout must be a finite positive number"
        ) from exc
    if (
        isinstance(cancellation_cleanup_timeout, bool)
        or not math.isfinite(cleanup_timeout)
        or cleanup_timeout <= 0
    ):
        raise ValueError(
            "cancellation_cleanup_timeout must be a finite positive number"
        )

    task = replace(
        task,
        task_id=task_id,
        timeout=task_timeout,
        max_tokens=task_max_tokens,
        extras=dict(task.extras) if task.extras is not None else None,
        harness_artifact_paths=harness_artifact_inputs,
    )
    max_steps = normalized_max_steps
    start = time.monotonic()
    task_deadline = start + task_timeout
    ensure_directory_no_symlinks(output_dir)
    trajectories_dir = os.path.join(output_dir, "trajectories")
    # Workflow mode gets its own per-task run folder (mirroring a team / CLI
    # workflow run): per-role ``<seq>_<role>.json`` conversations + one
    # ``orchestration.jsonl`` for the scheduling/step signals + a ``workflow.json``
    # manifest. Single-session mode is unchanged — one flat ``<task_id>.jsonl``.
    run_dir: str | None = None
    if workflow is None:
        tracer = Tracer(run_id=task.task_id, output_dir=trajectories_dir)
    else:
        run_dir = os.path.join(trajectories_dir, task.task_id)
        tracer = Tracer(
            run_id=task.task_id, output_dir=run_dir, filename=ORCHESTRATION_FILENAME
        )

    env: Environment | None = None
    session: Session | None = None
    workflow_ctx: WorkflowContext | None = None
    session_holder: list[Session] = []
    workflow_context_holder: list[WorkflowContext] = []
    owned_execution_tasks: list[asyncio.Task[Any]] = []
    stage_tasks: dict[str, asyncio.Task[Any]] = {}
    observed_stage_results: set[str] = set()
    environment_setup_owner: _EnvironmentSetupOwner | None = None
    checkpoint: WorktreeCheckpoint | None = None
    checkpoint_result: dict[str, Any] | None = None
    error: str | None = None
    cancellation: asyncio.CancelledError | None = None
    patch = ""
    # Paths of any injected benchmark test files — checked out before patch
    # extraction so they never contaminate the submitted model_patch.
    injected_paths: list[str] = []
    harness_artifact_paths: list[str] = []
    harness_artifact_exclusion_proven = True
    checkpoint_restore_integrity_proven = True
    test_patch_isolation_failed = False
    task_stage_integrity_proven = True
    persistence_succeeded = True
    final_snapshot_lingering: tuple[asyncio.Task[Any], ...] = ()
    manifest_lingering: tuple[asyncio.Task[Any], ...] = ()

    def remaining_task_time() -> float:
        remaining = task_deadline - time.monotonic()
        if remaining <= 0:
            raise CallerTimeoutError
        return remaining

    async def await_task_stage(
        stage_name: str,
        awaitable: Awaitable[Any],
    ) -> Any:
        nonlocal task_stage_integrity_proven
        stage_task = asyncio.ensure_future(awaitable)
        owned_execution_tasks.append(stage_task)
        stage_tasks[stage_name] = stage_task
        try:
            result = await abandon_on_timeout(
                stage_task,
                remaining_task_time(),
            )
        except CallerTimeoutError:
            # The task still owns any value it may return while consuming
            # cancellation. Teardown adopts that late value after quiescence.
            task_stage_integrity_proven = False
            if not stage_task.done():
                stage_task.cancel()
            raise
        except BaseException:
            if stage_task.done():
                observed_stage_results.add(stage_name)
            raise
        observed_stage_results.add(stage_name)
        return result

    try:
        # Create environment (inside try — docker setup can fail)
        environment_setup_timeout = remaining_task_time()
        environment_setup_owner = _EnvironmentSetupOwner(
            env_factory,
            task,
            cleanup_timeout=cleanup_timeout,
        )
        owned_execution_tasks.append(environment_setup_owner.task)
        try:
            env = await environment_setup_owner.acquire(environment_setup_timeout)
        except CallerTimeoutError:
            task_stage_integrity_proven = False
            environment_setup_owner.relinquish()
            raise
        except BaseException:
            environment_setup_owner.relinquish()
            raise
        environment_setup_owner.transfer(env)
        remaining_task_time()
        tools = list(tools_factory())
        remaining_task_time()
        artifact_candidates: list[str | os.PathLike[str]] = list(
            task.harness_artifact_paths
        )
        output_relative = _workspace_relative_host_path(env, output_dir)
        if output_relative is not None and output_relative != Path("."):
            artifact_candidates.append(output_dir)
        else:
            artifact_candidates.extend(
                (
                    trajectories_dir,
                    os.path.join(output_dir, "results.jsonl"),
                    os.path.join(output_dir, RESULT_TEMP_DIRECTORY),
                )
            )
            if output_relative == Path("."):
                legacy_paths, legacy_scan_complete = _legacy_result_temp_paths(
                    output_dir
                )
                artifact_candidates.extend(legacy_paths)
                harness_artifact_exclusion_proven = legacy_scan_complete
        harness_artifact_paths = _workspace_relative_artifact_paths(
            env,
            artifact_candidates,
        )
        artifact_bound_error = _mapped_artifact_path_bound_error(
            harness_artifact_paths
        )
        if artifact_bound_error:
            harness_artifact_exclusion_proven = False
            raise RuntimeError(artifact_bound_error)
        if not harness_artifact_exclusion_proven:
            raise RuntimeError(
                "legacy result temp artifact scan exceeded its safety bound"
            )

        if run_dir is not None and normalized_checkpoint_interval is not None:
            checkpoint = WorktreeCheckpoint(
                Path(run_dir),
                interval_seconds=normalized_checkpoint_interval,
            )
            if resume_from_checkpoint:
                restore_result = await await_task_stage(
                    "checkpoint_restore",
                    checkpoint.restore_latest(
                        env,
                        exclude_paths=harness_artifact_paths,
                    )
                )
                checkpoint_result = {
                    "restore": restore_result.to_dict()
                }
                checkpoint_restore_integrity_proven = (
                    restore_result.worktree_integrity_proven
                )
                if not checkpoint_restore_integrity_proven:
                    raise RuntimeError(
                        "checkpoint restore left worktree integrity unproven"
                    )

        # SWE-bench test injection: apply the real FAIL_TO_PASS test into the
        # workspace BEFORE the workflow runs so the agent can verify against it.
        # Guarded on extras so single-session / non-SWE-bench paths are
        # unaffected. Preflight failures skip injection. An uncertain rollback
        # stops before agent execution and preserves known paths for final
        # cleanup and temporary-index exclusion.
        test_patch = (task.extras or {}).get("test_patch")
        if test_patch:
            try:
                injected_paths = await await_task_stage(
                    "test_patch_injection",
                    apply_test_patch(env, test_patch)
                )
            except TestPatchIsolationError as exc:
                injected_paths = list(dict.fromkeys(exc.touched_paths))
                test_patch_isolation_failed = True
                if exc.cancellation is not None:
                    raise exc.cancellation from exc
                raise

        if checkpoint is not None:
            remaining_task_time()
            await checkpoint.start(
                env,
                exclude_paths=(*injected_paths, *harness_artifact_paths),
            )

        # Orientation up front: a bounded repo map in the system prompt saves
        # the model its first N steps of ls/find exploration.
        repo_map = await await_task_stage(
            "repo_map",
            build_repo_map_via_env(env),
        )
        prompt = f"{prompt}\n\n{repo_map}" if repo_map else prompt

        execution_task = replace(task, timeout=remaining_task_time())

        if workflow is None:
            session = await _run_single_session(
                task=execution_task,
                env=env,
                tracer=tracer,
                prompt=prompt,
                tools=tools,
                model=model,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                max_steps=max_steps,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                thinking_params=thinking_params,
                session_holder=session_holder,
                owned_tasks=owned_execution_tasks,
            )
        else:
            workflow_ctx = await _run_workflow_mode(
                task=execution_task,
                env=env,
                tracer=tracer,
                prompt=prompt,
                tools=tools,
                model=model,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                max_steps=max_steps,
                workflow=workflow,
                injected_paths=injected_paths,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                thinking_params=thinking_params,
                save_dir=run_dir,
                context_holder=workflow_context_holder,
                owned_tasks=owned_execution_tasks,
                timeout_error_seconds=task.timeout,
            )

    except asyncio.CancelledError as exc:
        # Finish the same checkpoint/diff/tracer/environment teardown below,
        # then propagate cancellation after every owned resource is released.
        cancellation = exc
        if getattr(exc, "checkpoint_restore_integrity_proven", True) is False:
            checkpoint_restore_integrity_proven = False
        error = "evaluation task cancelled"
    except CallerTimeoutError:
        error = f"Task timed out after {task.timeout}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    async def await_teardown(awaitable: Awaitable[Any]) -> Any:
        """Finish one owned teardown operation despite repeated caller cancel."""
        nonlocal cancellation, error
        owned_task = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(owned_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                    error = _append_harness_error(
                        error,
                        "evaluation task cancelled",
                        RuntimeError("caller cancelled during teardown"),
                    )
                if owned_task.done():
                    return owned_task.result()
                continue

    if session is None and session_holder:
        session = session_holder[0]
    if workflow_ctx is None and workflow_context_holder:
        workflow_ctx = workflow_context_holder[0]
    execution_quiesced = await await_teardown(
        _wait_for_owned_execution(
            owned_execution_tasks,
            workflow_ctx,
            cleanup_timeout=cleanup_timeout,
        )
    )

    if environment_setup_owner is not None:
        for stage, disposal_error in environment_setup_owner.disposal_errors:
            error = _append_harness_error(error, stage, disposal_error)
            execution_quiesced = False

    if execution_quiesced and workflow_ctx is not None:
        try:
            (
                final_snapshots_quiesced,
                persistence_errors,
                final_snapshot_lingering,
            ) = await await_teardown(
                _finalize_eval_workflow_sessions(
                    workflow_ctx,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            persistence_succeeded = False
            final_snapshots_quiesced = False
            final_snapshot_lingering = tuple(
                task
                for task in workflow_ctx.pending_cleanup_tasks
                if isinstance(task, asyncio.Task) and not task.done()
            )
            error = _append_harness_error(
                error,
                "final workflow snapshot failed",
                exc,
            )
        else:
            for persistence_error in persistence_errors:
                persistence_succeeded = False
                error = _append_harness_error(
                    error,
                    "final workflow snapshot failed",
                    persistence_error,
                )
        if not final_snapshots_quiesced:
            persistence_succeeded = False
            execution_quiesced = False
            error = _append_harness_error(
                error,
                "final workflow snapshot timed out",
                TimeoutError("session persistence owner remained active"),
            )

    # A stage may consume cancellation and return a resource or mutation record
    # after its caller-side deadline. Adopt every such result before deciding
    # what must be cleaned or excluded from the candidate diff.
    if execution_quiesced:
        for stage_name, stage_task in stage_tasks.items():
            if stage_name in observed_stage_results:
                continue
            observed_stage_results.add(stage_name)
            if stage_task.cancelled():
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
                continue
            try:
                late_value = stage_task.result()
            except TestPatchIsolationError as exc:
                if stage_name == "test_patch_injection":
                    injected_paths = list(
                        dict.fromkeys((*injected_paths, *exc.touched_paths))
                    )
                    test_patch_isolation_failed = True
                error = _append_harness_error(
                    error,
                    f"late {stage_name} failed",
                    exc,
                )
            except asyncio.CancelledError:
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
            except BaseException as exc:
                error = _append_harness_error(
                    error,
                    f"late {stage_name} failed",
                    exc,
                )
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
            else:
                if stage_name == "checkpoint_restore":
                    restore_result = late_value
                    if checkpoint_result is None:
                        checkpoint_result = {}
                    checkpoint_result["restore"] = restore_result.to_dict()
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    injected_paths = list(
                        dict.fromkeys((*injected_paths, *late_value))
                    )
                    test_patch_isolation_failed = True

    if not execution_quiesced:
        failure = TimeoutError(
            "owned execution did not quiesce after cancellation; "
            "patch extraction skipped"
        )
        error = _append_harness_error(error, "execution cleanup timed out", failure)
        if checkpoint is not None:
            try:
                checkpoint_quiesced = await await_teardown(
                    checkpoint.abort(timeout=cleanup_timeout)
                )
            except Exception as exc:
                checkpoint_quiesced = False
                error = _append_harness_error(
                    error,
                    "checkpoint abort failed",
                    exc,
                )
            if checkpoint_result is None:
                checkpoint_result = {}
            checkpoint_result["abort"] = {
                "status": (
                    "aborted_non_quiescent_execution"
                    if checkpoint_quiesced
                    else "checkpoint_abort_timed_out"
                )
            }
            if not checkpoint_quiesced:
                error = _append_harness_error(
                    error,
                    "checkpoint abort timed out",
                    TimeoutError("periodic checkpoint capture remained active"),
                )
        if env is not None:
            try:
                environment_quiesced = await await_teardown(
                    _abort_environment(
                        env,
                        cleanup_timeout=cleanup_timeout,
                    )
                )
                if not environment_quiesced:
                    error = _append_harness_error(
                        error,
                        "environment abort timed out",
                        TimeoutError("environment abort hook remained active"),
                    )
            except Exception as exc:
                error = _append_harness_error(error, "environment abort failed", exc)

    if (
        execution_quiesced
        and env
        and checkpoint is not None
        and (
            test_patch_isolation_failed
            or not checkpoint_restore_integrity_proven
        )
    ):
        try:
            checkpoint_quiesced = await await_teardown(
                checkpoint.abort(timeout=cleanup_timeout)
            )
        except Exception as exc:
            checkpoint_quiesced = False
            error = _append_harness_error(error, "checkpoint abort failed", exc)
        if checkpoint_result is None:
            checkpoint_result = {}
        checkpoint_result["final"] = {
            "status": (
                (
                    "skipped_test_patch_isolation_failure"
                    if test_patch_isolation_failed
                    else "skipped_checkpoint_restore_integrity_failure"
                )
                if checkpoint_quiesced
                else "checkpoint_abort_timed_out"
            )
        }
        if not checkpoint_quiesced:
            error = _append_harness_error(
                error,
                "checkpoint abort timed out",
                TimeoutError("periodic checkpoint capture remained active"),
            )
            execution_quiesced = False

    if (
        execution_quiesced
        and env
        and checkpoint is not None
        and not test_patch_isolation_failed
        and checkpoint_restore_integrity_proven
    ):
        try:
            checkpoint_finalized, final_checkpoint = await await_teardown(
                _stop_checkpoint_bounded(
                    checkpoint,
                    env,
                    exclude_paths=(*injected_paths, *harness_artifact_paths),
                    cleanup_timeout=cleanup_timeout,
                )
            )
            if checkpoint_result is None:
                checkpoint_result = {}
            if checkpoint_finalized:
                checkpoint_result["final"] = final_checkpoint.to_dict()
            else:
                checkpoint_result["final"] = {
                    "status": "checkpoint_finalization_timed_out"
                }
                error = _append_harness_error(
                    error,
                    "checkpoint finalization timed out",
                    TimeoutError("checkpoint stop remained active"),
                )
                try:
                    checkpoint_quiesced = await await_teardown(
                        checkpoint.abort(timeout=cleanup_timeout)
                    )
                    if not checkpoint_quiesced:
                        error = _append_harness_error(
                            error,
                            "checkpoint abort timed out",
                            TimeoutError("periodic checkpoint capture remained active"),
                        )
                except Exception as exc:
                    error = _append_harness_error(
                        error,
                        "checkpoint abort failed",
                        exc,
                    )
                abort_quiesced = await await_teardown(
                    _abort_environment(
                        env,
                        cleanup_timeout=cleanup_timeout,
                    )
                )
                if not abort_quiesced:
                    error = _append_harness_error(
                        error,
                        "environment abort timed out",
                        TimeoutError("environment abort hook remained active"),
                    )
                execution_quiesced = False
        except Exception as exc:
            error = _append_harness_error(error, "checkpoint finalization failed", exc)

    injected_path_cleanup_proven = not injected_paths

    # Diff-exclusion (load-bearing): revert any injected benchmark test files
    # before extracting the patch so the submitted model_patch NEVER contains the
    # test edits. The grader applies its own test_patch; leaving these in would
    # cause a double-apply / conflict at grading time.
    #
    # Done per-path so a single failure can't abort the rest, and a test_patch
    # that ADDS a new file (the common SWE-bench case) is handled too. The real
    # SWE-bench driver extracts via ``git add -A && git diff --cached``, so an
    # untracked injected file would otherwise be staged and leak: ``git checkout``
    # only restores TRACKED paths and errors on an untracked one, so we follow it
    # with ``git clean -fq`` to remove any still-untracked (newly added) injected
    # file. ``git clean`` is a no-op on a path with no untracked content, so it is
    # safe to run unconditionally for both modified-existing and added files.
    if execution_quiesced and env and injected_paths:
        injected_path_cleanup_proven = True
        injected_cleanup_deadline = time.monotonic() + cleanup_timeout
        for index, path in enumerate(injected_paths):
            remaining_cleanup = injected_cleanup_deadline - time.monotonic()
            if remaining_cleanup <= 0:
                injected_path_cleanup_proven = False
                error = _append_harness_error(
                    error,
                    "test patch cleanup failed",
                    TimeoutError(
                        "aggregate cleanup deadline expired with "
                        f"{len(injected_paths) - index} path(s) remaining"
                    ),
                )
                break
            quoted = shlex.quote(path)

            async def run_cleanup_command(command: str) -> Any:
                remaining = injected_cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("aggregate injected-path cleanup deadline expired")
                return await await_teardown(
                    env.exec_cmd(command, timeout=min(cleanup_timeout, remaining))
                )

            try:
                await run_cleanup_command(
                    f"git --literal-pathspecs checkout -- {quoted}"
                )
                await run_cleanup_command(
                    f"git --literal-pathspecs clean -fq -- {quoted}"
                )
                status = await run_cleanup_command(
                    "git --literal-pathspecs status --porcelain=v1 -z -- "
                    f"{quoted}"
                )
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
                        f"injected path still dirty: {path}"
                        + (f": {detail[:500]}" if detail else "")
                    )
                    error = _append_harness_error(
                        error, "test patch cleanup failed", failure
                    )
                    injected_path_cleanup_proven = False
            except Exception as exc:
                error = _append_harness_error(error, "test patch cleanup failed", exc)
                injected_path_cleanup_proven = False

    # Extract git patch (best-effort even after errors). Use a temporary index so
    # new files are included without mutating the environment's real git index.
    patch_extraction_succeeded = False
    if execution_quiesced and env:
        try:
            patch_result = await await_teardown(
                env.exec_cmd(
                    worktree_diff_command(
                        (*injected_paths, *harness_artifact_paths)
                    )
                )
            )
            patch = patch_result.stdout
            if patch_result.stdout_truncated or patch_result.stderr_truncated:
                patch = ""
                failure = RuntimeError(
                    "diff output truncated: "
                    f"stdout dropped {patch_result.stdout_dropped_bytes} bytes, "
                    f"stderr dropped {patch_result.stderr_dropped_bytes} bytes"
                )
                error = _append_harness_error(error, "patch extraction failed", failure)
            elif patch_result.returncode != 0:
                patch = ""
                detail = (patch_result.stderr or "").strip()
                failure = RuntimeError(
                    f"diff command exited {patch_result.returncode}"
                    + (f": {detail[:500]}" if detail else "")
                )
                error = _append_harness_error(error, "patch extraction failed", failure)
            else:
                patch_extraction_succeeded = True
                if (
                    test_patch_isolation_failed
                    or not harness_artifact_exclusion_proven
                    or not checkpoint_restore_integrity_proven
                    or not task_stage_integrity_proven
                ):
                    patch = ""
        except Exception as exc:
            error = _append_harness_error(error, "patch extraction failed", exc)

    if (
        execution_quiesced
        and workflow_ctx is not None
        and workflow is not None
        and run_dir is not None
    ):
        try:
            (
                manifest_quiesced,
                manifest_error,
                manifest_lingering,
            ) = await await_teardown(
                _persist_eval_workflow_manifest_owned(
                    run_dir,
                    task=task,
                    workflow=workflow,
                    ctx=workflow_ctx,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            persistence_succeeded = False
            error = _append_harness_error(
                error,
                "workflow manifest failed",
                exc,
            )
        else:
            if manifest_error is not None:
                persistence_succeeded = False
                error = _append_harness_error(
                    error,
                    "workflow manifest failed",
                    manifest_error,
                )
            if not manifest_quiesced:
                persistence_succeeded = False
                execution_quiesced = False
                error = _append_harness_error(
                    error,
                    "workflow manifest timed out",
                    TimeoutError("manifest persistence owner remained active"),
                )

    live_resource_dependencies: set[asyncio.Task[Any]] = {
        task
        for task in (
            *owned_execution_tasks,
            *final_snapshot_lingering,
            *manifest_lingering,
        )
        if not task.done()
    }
    if workflow_ctx is not None:
        live_resource_dependencies.update(
            task
            for task in workflow_ctx.pending_cleanup_tasks
            if isinstance(task, asyncio.Task) and not task.done()
        )
    resources_deferred = bool(live_resource_dependencies)
    if resources_deferred:
        execution_quiesced = False
        _defer_eval_resource_cleanup(
            tuple(live_resource_dependencies),
            tracer=tracer,
            timeout=cleanup_timeout,
        )
        error = _append_harness_error(
            error,
            "resource cleanup deferred",
            TimeoutError("owned persistence or execution task remained active"),
        )

    duration = time.monotonic() - start
    if not resources_deferred:
        try:
            tracer.close()
        except Exception as exc:
            error = _append_harness_error(error, "tracer close failed", exc)
    tracer_write_error = getattr(tracer, "write_error", None)
    if tracer_write_error:
        error = _append_harness_error(
            error,
            "tracer write failed",
            RuntimeError(str(tracer_write_error)),
        )

    if env:
        environment_cleaned = False
        cleanup_raised = False
        try:
            environment_cleaned = await await_teardown(
                _cleanup_environment_bounded(
                    env,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            cleanup_raised = True
            error = _append_harness_error(error, "environment cleanup failed", exc)
        if not environment_cleaned:
            execution_quiesced = False
            patch = ""
            patch_extraction_succeeded = False
            if not cleanup_raised:
                error = _append_harness_error(
                    error,
                    "environment cleanup timed out",
                    TimeoutError("environment cleanup hook remained active"),
                )
            try:
                abort_quiesced = await await_teardown(
                    _abort_environment(
                        env,
                        cleanup_timeout=cleanup_timeout,
                    )
                )
                if not abort_quiesced:
                    error = _append_harness_error(
                        error,
                        "environment abort timed out",
                        TimeoutError("environment abort hook remained active"),
                    )
            except Exception as exc:
                error = _append_harness_error(error, "environment abort failed", exc)

    if workflow_ctx is not None:
        sessions = workflow_ctx.sessions
        tokens_used = _aggregate_tokens(sessions)
        steps = _aggregate_steps(sessions)
        markup_recovered = _aggregate_markup_recovery(sessions)
        workflow_error = getattr(workflow_ctx, "workflow_error", None)
        if workflow_error:
            error = f"{workflow_error}; {error}" if error else workflow_error
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0
        markup_recovered = getattr(session, "markup_recovered", 0) if session else 0

    result = EvalResult(
        task_id=task.task_id,
        patch=patch,
        # An on-disk diff is a real, submittable patch regardless of how the run
        # ended — a budget-floor stop or an outer-wall timeout still produces the
        # patch we grade (django-11564 was graded RESOLVED yet reported
        # patch_produced=false under the old ``and error is None`` guard).
        patch_produced=bool(patch.strip()),
        tokens_used=tokens_used,
        steps=steps,
        duration=duration,
        error=error,
        trajectory_path=tracer.path,
        markup_recovered=markup_recovered,
        workflow_result=getattr(workflow_ctx, "workflow_result", None) if workflow_ctx else None,
        checkpoint_result=checkpoint_result,
        test_patch_isolation_failed=test_patch_isolation_failed,
        execution_quiesced=execution_quiesced,
        patch_extraction_succeeded=patch_extraction_succeeded,
        injected_path_cleanup_proven=injected_path_cleanup_proven,
        harness_artifact_exclusion_proven=harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=(
            checkpoint_restore_integrity_proven
        ),
        task_stage_integrity_proven=task_stage_integrity_proven,
        submission_eligible=(
            execution_quiesced
            and patch_extraction_succeeded
            and injected_path_cleanup_proven
            and harness_artifact_exclusion_proven
            and checkpoint_restore_integrity_proven
            and task_stage_integrity_proven
            and persistence_succeeded
            and not test_patch_isolation_failed
        ),
    )
    if cancellation is not None:
        if error:
            add_note = getattr(cancellation, "add_note", None)
            if callable(add_note):
                add_note(f"evaluation teardown diagnostics: {error}")
        raise cancellation
    return result


async def run_eval_batch(
    tasks: list[EvalTask],
    concurrency: int = 4,
    **kwargs,
) -> list[EvalResult]:
    """Run multiple evaluation tasks with controlled concurrency.

    Individual task failures produce an EvalResult with error set,
    rather than aborting the entire batch.
    """
    if isinstance(concurrency, bool):
        raise ValueError("concurrency must be a positive integer")
    try:
        concurrency = operator.index(concurrency)
    except TypeError as exc:
        raise ValueError("concurrency must be a positive integer") from exc
    if concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    task_ids = [_validate_task_id(task.task_id) for task in tasks]
    collision_keys = [_task_id_collision_key(task_id) for task_id in task_ids]
    if len(set(collision_keys)) != len(collision_keys):
        raise ValueError("task_id values must be unique within an evaluation batch")
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(task: EvalTask) -> EvalResult:
        async with semaphore:
            try:
                return await run_eval_task(task, **kwargs)
            except Exception as e:
                return EvalResult(
                    task_id=task.task_id,
                    patch="",
                    patch_produced=False,
                    tokens_used=0,
                    steps=0,
                    duration=0.0,
                    error=f"Unhandled: {type(e).__name__}: {e}",
                    execution_quiesced=False,
                    patch_extraction_succeeded=False,
                    injected_path_cleanup_proven=False,
                    harness_artifact_exclusion_proven=False,
                    checkpoint_restore_integrity_proven=False,
                    task_stage_integrity_proven=False,
                    submission_eligible=False,
                )

    coros = [run_one(t) for t in tasks]
    results = await asyncio.gather(*coros)
    return results


def _fsync_directory_fds(directory_fds: Sequence[int]) -> None:
    first_failure: BaseException | None = None
    for directory_fd in directory_fds:
        try:
            os.fsync(directory_fd)
        except BaseException as exc:
            if first_failure is None:
                first_failure = exc
            else:
                add_note = getattr(first_failure, "add_note", None)
                if callable(add_note):
                    add_note(
                        "additional directory fsync failure: "
                        f"{type(exc).__name__}: {exc}"
                    )
    if first_failure is not None:
        raise first_failure


def save_results(results: list[EvalResult], output_path: str) -> None:
    """Durably save evaluation results and their recoverable patches as JSONL."""
    target = Path(os.path.abspath(output_path))
    if not target.name or target.name in {".", ".."}:
        raise ValueError("output_path must name a results file")
    ensure_directory_no_symlinks(target.parent)
    temp_directory = target.parent / RESULT_TEMP_DIRECTORY
    ensure_directory_no_symlinks(temp_directory)
    temporary = f".oc-{uuid.uuid4().hex}.tmp"
    parent_fd = -1
    temp_directory_fd = -1
    fd = -1
    replaced = False
    written_identity: tuple[int, int] | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        parent_fd = _open_directory_no_symlinks(target.parent)
        temp_directory_fd = _open_directory_no_symlinks(temp_directory)
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError("results parent changed before temporary-file creation")
        if not _directory_path_matches_fd(temp_directory, temp_directory_fd):
            raise OSError("results temporary directory changed before creation")
        try:
            existing = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"results target is not a regular file: {target}")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=temp_directory_fd,
        )
        temporary_info = os.fstat(fd)
        temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            total_bytes = 0
            for r in results:
                record = {
                    "task_id": r.task_id,
                    "patch": r.patch,
                    "patch_produced": r.patch_produced,
                    "tokens_used": r.tokens_used,
                    "steps": r.steps,
                    "duration": round(r.duration, 2),
                    "error": r.error,
                    "patch_lines": len(r.patch.splitlines()),
                    "trajectory": r.trajectory_path,
                    "test_patch_isolation_failed": r.test_patch_isolation_failed,
                    "execution_quiesced": r.execution_quiesced,
                    "patch_extraction_succeeded": r.patch_extraction_succeeded,
                    "injected_path_cleanup_proven": r.injected_path_cleanup_proven,
                    "harness_artifact_exclusion_proven": (
                        r.harness_artifact_exclusion_proven
                    ),
                    "checkpoint_restore_integrity_proven": (
                        r.checkpoint_restore_integrity_proven
                    ),
                    "task_stage_integrity_proven": (
                        r.task_stage_integrity_proven
                    ),
                    "submission_eligible": r.submission_eligible,
                }
                if r.checkpoint_result is not None:
                    record["checkpoint_result"] = r.checkpoint_result
                line = json.dumps(record) + "\n"
                line_bytes = len(line.encode("utf-8"))
                if line_bytes > MAX_RESULT_RECORD_BYTES:
                    raise ValueError(
                        f"evaluation result record exceeds {MAX_RESULT_RECORD_BYTES} bytes"
                    )
                total_bytes += line_bytes
                if total_bytes > MAX_RESULTS_FILE_BYTES:
                    raise ValueError(
                        f"evaluation results exceed {MAX_RESULTS_FILE_BYTES} bytes"
                    )
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            written_identity = (written.st_dev, written.st_ino)
        if not _directory_path_matches_fd(target.parent, parent_fd):
            raise OSError("results parent changed before atomic replace")
        if not _directory_path_matches_fd(temp_directory, temp_directory_fd):
            raise OSError("results temporary directory changed before atomic replace")
        os.replace(
            temporary,
            target.name,
            src_dir_fd=temp_directory_fd,
            dst_dir_fd=parent_fd,
        )
        replaced = True
        _fsync_directory_fds((temp_directory_fd, parent_fd))
        verified_parent_fd = _open_directory_no_symlinks(target.parent)
        try:
            original_parent = os.fstat(parent_fd)
            verified_parent = os.fstat(verified_parent_fd)
            if (original_parent.st_dev, original_parent.st_ino) != (
                verified_parent.st_dev,
                verified_parent.st_ino,
            ):
                raise OSError("results parent changed after atomic replace")
            current = os.stat(
                target.name,
                dir_fd=verified_parent_fd,
                follow_symlinks=False,
            )
            if (
                written_identity is None
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != written_identity
            ):
                raise OSError("results target changed after atomic replace")
        finally:
            os.close(verified_parent_fd)
    except BaseException as primary_error:
        if fd >= 0:
            try:
                os.close(fd)
            except BaseException as cleanup_error:
                primary_error.add_note(
                    "results temporary fd cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        if temp_directory_fd >= 0 and not replaced:
            try:
                current = os.stat(
                    temporary,
                    dir_fd=temp_directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except BaseException as cleanup_error:
                primary_error.add_note(
                    "results temporary identity cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            else:
                if temporary_identity is None or (
                    current.st_dev,
                    current.st_ino,
                ) != temporary_identity:
                    primary_error.add_note(
                        "results temporary cleanup skipped because ownership changed"
                    )
                else:
                    try:
                        os.unlink(temporary, dir_fd=temp_directory_fd)
                    except BaseException as cleanup_error:
                        primary_error.add_note(
                            "results temporary unlink cleanup failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
        raise
    finally:
        if temp_directory_fd >= 0:
            os.close(temp_directory_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
