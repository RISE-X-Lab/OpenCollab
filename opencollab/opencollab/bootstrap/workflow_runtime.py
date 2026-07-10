"""Bootstrap wiring for the mini workflow engine.

Binds the application-layer :class:`~opencollab.application.workflow.WorkflowContext`
to the concrete ``build_session`` machinery: :class:`WorkflowSessionFactory`
implements ``WorkflowSessionFactoryPort`` by assembling a one-shot ``Agent`` +
``Session`` per ``ctx.agent`` call, with the resolved model / provider / key /
base-url flowing through.

Also owns workflow *discovery* — loading ``@workflow``-decorated functions from a
directory of python files via importlib — and the ``run_workflow`` entry point
that builds a context, runs the workflow function, and returns its result. This
is composition-root code (it knows concrete types), so it lives in bootstrap.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import os
import stat
import types
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safe_files import read_regular_text
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.async_timeout import isolate_tasks_from_shutdown
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
)
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    build_session,
    slug_label,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent

# System prompt seeded into every one-shot workflow agent. Deliberately terse:
# the workflow's per-call prompt carries the actual task.
WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)

DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS = 2.0
_LATE_TRACER_OWNER_TASKS: set[asyncio.Task[Any]] = set()
_LATE_TRACER_FAILURES: deque[BaseException] = deque(maxlen=64)
MAX_WORKFLOW_DIRECTORY_ENTRIES = 4_096
MAX_WORKFLOW_FILES = 256
MAX_WORKFLOW_SOURCE_BYTES = 4 * 1024 * 1024
_WORKFLOW_MANIFEST_OWNER_TASKS: set[asyncio.Task[Any]] = set()

# Back-compat alias: the slug helper now lives in ``session_factory`` so the
# eval harness can share it. Kept under its original private name here.
_slug = slug_label


class WorkflowSessionFactory:
    """``WorkflowSessionFactoryPort`` bound to the concrete ``build_session``.

    Each ``build_workflow_session`` call assembles a fresh one-shot ``Agent``
    (carrying the resolved LLM config) and a self-wiring ``Session``. ``tools``
    from the caller become the agent's toolset; ``isolation`` is accepted for
    forward-compatibility (a future worktree-backed environment) but currently
    runs in a local environment like the headless evaluator.
    """

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        workspace: str | None = None,
        tracer: TracePort | None = None,
        event_sink: EventPublisherPort | None = None,
        llm_timeout: float = 600.0,
        temperature: float = DEFAULT_TEMPERATURE,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        save_dir: str | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._api_key = api_key
        self._base_url = base_url
        self._workspace = workspace
        self._tracer = tracer
        self._event_sink = event_sink
        self._llm_timeout = llm_timeout
        self._temperature = temperature
        self._thinking = thinking
        self._thinking_params = (
            thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        )
        # Run folder where each one-shot session's transcript is autosaved. When
        # set, every ``build_workflow_session`` gets its own ``<seq>_<role>.json``
        # so the AutoSaveSubscriber (wired by ``build_session`` once an
        # ``auto_save_path`` is present) persists it — the same per-role mechanism
        # chat/team sessions use. ``None`` keeps sessions ephemeral (the prior
        # behaviour).
        self._save_dir = save_dir
        self._session_seq = 0

    def _next_save_path(self, label: str | None) -> str | None:
        """Per-session transcript path: ``<save_dir>/<seq>_<role>.json``.

        Returns ``None`` when no run folder is configured. The sequence number
        orders sessions by creation and guarantees uniqueness; incrementing it
        has no ``await`` so it is atomic under the event loop's cooperative
        scheduling even when ``parallel``/``pipeline`` build many sessions
        concurrently. The caller's ``label`` (e.g. ``coder:s1r2``) is slugged
        into the name so a run folder reads as its workflow phases at a glance.
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
    ) -> Any:
        use_thinking = self._thinking if thinking is None else thinking
        agent = Agent(
            name="workflow_agent",
            system_prompt=WORKFLOW_AGENT_PROMPT,
            tools=list(tools or []),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        env = LocalEnvironment(self._workspace) if self._workspace else LocalEnvironment()
        return build_session(
            agent=agent,
            env=env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            event_sink=self._event_sink,
            llm_timeout=self._llm_timeout,
            auto_save_path=self._next_save_path(label),
        )


def build_workflow_context(
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    save_dir: str | None = None,
) -> WorkflowContext:
    """Build a :class:`WorkflowContext` wired to the concrete session factory.

    ``cfg`` is the resolved config dict (``model`` / ``provider`` / ``api_key`` /
    ``base_url`` / ``budget`` / optional ``llm_timeout`` / ``temperature``)
    produced by the CLI's
    file-first config resolution — so a stale shell ``ANTHROPIC_API_KEY`` cannot
    shadow the configured key. ``budget`` overrides ``cfg['budget']`` when given;
    ``None`` for an unbounded workflow. ``save_dir``, when given, is the run
    folder each session's transcript is autosaved into; ``None`` keeps sessions
    ephemeral.
    """
    factory = WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        workspace=workspace,
        tracer=tracer,
        event_sink=event_sink,
        llm_timeout=float(cfg.get("llm_timeout", 600.0)),
        temperature=float(cfg.get("temperature", DEFAULT_TEMPERATURE)),
        thinking=bool(cfg.get("thinking", DEFAULT_THINKING)),
        thinking_params=cfg.get("thinking_params") or dict(DEFAULT_THINKING_PARAMS),
        save_dir=save_dir,
    )
    budget_total = budget if budget is not None else cfg.get("budget")
    # Working-tree probe over the same workspace the sessions edit, so the
    # workflow can verify a real edit landed before declaring success.
    probe_env = LocalEnvironment(workspace) if workspace else LocalEnvironment()
    return WorkflowContext(
        factory,
        event_sink=event_sink,
        tracer=tracer,
        max_concurrency=max_concurrency,
        budget_total=budget_total,
        tree_probe=EnvWorkingTreeProbe(probe_env),
        workspace_root=workspace,
    )


def _resolve_spec_fn(spec_or_fn: Any) -> Any:
    """Return the callable workflow function from a spec or a raw function."""
    if isinstance(spec_or_fn, WorkflowSpec):
        return spec_or_fn.fn
    return spec_or_fn


async def run_workflow(
    spec_or_fn: Any,
    args: dict[str, Any],
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    save_dir: str | None = None,
    trace: bool = True,
    cleanup_timeout: float = DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
) -> Any:
    """Build a context, run the workflow function with ``args``, return its result.

    Accepts either a :class:`WorkflowSpec` or a raw ``@workflow``-decorated (or
    plain async) function.

    ``WorkflowBudgetExceeded`` — the sole exception ``WorkflowContext`` lets
    escape — is caught at this run boundary and turned into a structured result
    so the CLI prints a JSON budget report instead of a raw traceback::

        {"status": "budget_exceeded", "error": <str>,
         "tokens_spent": <int>, "budget_total": <int | None>}

    Every other exception still propagates to the caller.

    When ``save_dir`` is given the run folder mirrors a team run folder: each
    session's conversation is autosaved per role (``<seq>_<role>.json``) and a
    ``workflow.json`` manifest (workflow name, args, session count, spend) ties
    them together the way the team manifest groups a chat run's agents.

    A saved run also records the run's orchestration signals to a single
    ``<save_dir>/orchestration.jsonl`` (one ``workflow_phase`` / ``workflow_log``
    /  ``llm_call`` / ``tool_exec`` record per step, with tokens and latency) via
    an auto-wired :class:`Tracer` — the scheduling/step trace kept out of the
    per-role conversations. Pass ``trace=False`` to opt out, or supply your own
    ``tracer`` to keep ownership (it is then not auto-closed).

    ``cleanup_timeout`` bounds each shutdown phase. A timed-out session first
    receives a grace period, then all of its environments are synchronously
    revoked and their abort hooks are bounded. Persistence and owned-tracer
    closure happen only after every owned cleanup task is quiescent.
    """
    cleanup_timeout = _positive_cleanup_timeout(cleanup_timeout)
    fn = _resolve_spec_fn(spec_or_fn)
    name = spec_or_fn.name if isinstance(spec_or_fn, WorkflowSpec) else getattr(fn, "__name__", "workflow")

    # Own a Tracer only when saving, not opted out, and the caller didn't bring
    # one; close it in the finally below so the file handle is released even if
    # the workflow raises. A caller-supplied tracer keeps its own lifecycle. The
    # ``run_id`` is the workflow name (meaningful in each record); the on-disk
    # file is always ``orchestration.jsonl`` in the run folder.
    owns_tracer = tracer is None and save_dir is not None and trace
    if owns_tracer:
        tracer = Tracer(run_id=name, output_dir=save_dir, filename=ORCHESTRATION_FILENAME)

    try:
        ctx = build_workflow_context(
            cfg=cfg,
            workspace=workspace,
            tracer=tracer,
            event_sink=event_sink,
            budget=budget,
            max_concurrency=max_concurrency,
            save_dir=save_dir,
        )
    except BaseException as exc:
        if owns_tracer:
            tracer_close_failure = _close_tracer_capture(tracer)
            if tracer_close_failure is not None:
                _add_failure_note(
                    exc,
                    "owned workflow tracer close also failed: "
                    f"{type(tracer_close_failure).__name__}: "
                    f"{tracer_close_failure}",
                )
        raise
    cleanup_quiesced = False
    cleanup_succeeded = False
    lingering_cleanup_tasks: tuple[asyncio.Future[Any], ...] = ()
    workflow_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    first_cancellation: asyncio.CancelledError | None = None
    tracer_closed = False
    tracer_close_deferred = False
    tracer_failure: BaseException | None = None
    tracer_write_error: str | None = None
    tracer_dropped_steps = 0
    result: Any = None
    try:
        try:
            result = await fn(ctx, args)
        except WorkflowBudgetExceeded as exc:
            result = {
                "status": "budget_exceeded",
                "error": str(exc),
                "tokens_spent": ctx.budget.spent(),
                "budget_total": ctx.budget.total,
            }
        except BaseException as exc:
            workflow_failure = exc
            if isinstance(exc, asyncio.CancelledError):
                first_cancellation = exc

        cleanup_task = asyncio.create_task(
            _quiesce_and_finalize_workflow_context(
                ctx,
                timeout=cleanup_timeout,
            )
        )
        try:
            cleanup_result, cleanup_cancellation = (
                await _await_cleanup_despite_cancellation(cleanup_task)
            )
            (
                cleanup_quiesced,
                cleanup_succeeded,
                lingering_cleanup_tasks,
            ) = cleanup_result
            if first_cancellation is None:
                first_cancellation = cleanup_cancellation
        except BaseException as exc:
            cleanup_failure = exc
            lingering_cleanup_tasks = (cleanup_task,)

        (
            tracer_write_error,
            tracer_dropped_steps,
            tracer_inspection_failure,
        ) = _inspect_tracer(tracer)
        sticky_tracer_failure = _sticky_tracer_failure(
            tracer_write_error,
            tracer_dropped_steps,
        )
        if tracer_failure is None:
            tracer_failure = tracer_inspection_failure or sticky_tracer_failure

        early_exit = (
            first_cancellation is not None
            or cleanup_failure is not None
            or not cleanup_succeeded
            or workflow_failure is not None
        )
        if owns_tracer and cleanup_quiesced and early_exit:
            tracer_close_failure = _close_tracer_capture(tracer)
            tracer_closed = True
            tracer_failure = _merge_failure(
                tracer_failure,
                tracer_close_failure,
                note_prefix="workflow tracer close also failed",
            )

        if first_cancellation is not None:
            if cleanup_failure is not None:
                _add_failure_note(
                    first_cancellation,
                    "workflow cleanup also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}",
                )
            elif not cleanup_succeeded:
                _add_failure_note(
                    first_cancellation,
                    "workflow cleanup also failed: session cleanup, final "
                    "snapshot, or environment abort failed",
                )
            if tracer_failure is not None:
                _add_failure_note(
                    first_cancellation,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise first_cancellation
        if cleanup_failure is not None:
            failure = RuntimeError(
                "technical workflow cleanup failed: owned cleanup task failed"
            )
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            if workflow_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow execution also failed: "
                    f"{type(workflow_failure).__name__}: {workflow_failure}",
                )
            raise failure from cleanup_failure
        if not cleanup_succeeded:
            failure = RuntimeError(
                "technical workflow cleanup failed: session cleanup or "
                "environment abort failed"
            )
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from workflow_failure
        if workflow_failure is not None:
            if tracer_failure is not None:
                _add_failure_note(
                    workflow_failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise workflow_failure

        manifest_quiesced = True
        manifest_error: Exception | None = None
        manifest_failure: BaseException | None = None
        manifest_lingering_tasks: tuple[asyncio.Task[Any], ...] = ()
        manifest_cancellation: asyncio.CancelledError | None = None
        if save_dir is not None:
            manifest_task = asyncio.create_task(
                _persist_workflow_manifest_owned(
                    save_dir,
                    name=name,
                    args=args,
                    ctx=ctx,
                    tracer=tracer,
                    tracer_failure=tracer_failure,
                    tracer_write_error=tracer_write_error,
                    tracer_dropped_steps=tracer_dropped_steps,
                    timeout=cleanup_timeout,
                )
            )
            try:
                manifest_result, manifest_cancellation = (
                    await _await_manifest_despite_cancellation(manifest_task)
                )
                (
                    manifest_quiesced,
                    manifest_error,
                    manifest_lingering_tasks,
                ) = manifest_result
            except BaseException as exc:
                manifest_failure = exc
                if not manifest_task.done():
                    manifest_lingering_tasks = (manifest_task,)

        if owns_tracer and cleanup_quiesced:
            if manifest_lingering_tasks:
                _defer_owned_tracer_close(
                    ctx,
                    tracer,
                    (*lingering_cleanup_tasks, *manifest_lingering_tasks),
                    timeout=cleanup_timeout,
                )
                tracer_close_deferred = True
            else:
                tracer_close_failure = _close_tracer_capture(tracer)
                tracer_closed = True
                tracer_failure = _merge_failure(
                    tracer_failure,
                    tracer_close_failure,
                    note_prefix="workflow tracer close also failed",
                )

        if manifest_cancellation is not None:
            if manifest_failure is not None:
                _add_failure_note(
                    manifest_cancellation,
                    "workflow manifest persistence also failed: "
                    f"{type(manifest_failure).__name__}: {manifest_failure}",
                )
            elif not manifest_quiesced:
                _add_failure_note(
                    manifest_cancellation,
                    "workflow cleanup also failed: manifest persistence did "
                    "not quiesce",
                )
            elif manifest_error is not None:
                _add_failure_note(
                    manifest_cancellation,
                    "workflow manifest persistence also failed: "
                    f"{type(manifest_error).__name__}: {manifest_error}",
                )
            if tracer_failure is not None:
                _add_failure_note(
                    manifest_cancellation,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise manifest_cancellation
        if manifest_failure is not None:
            failure = RuntimeError(
                "technical workflow manifest persistence failed: owned "
                "manifest task failed"
            )
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from manifest_failure
        if not manifest_quiesced:
            failure = RuntimeError(
                "technical workflow cleanup failed: workflow manifest "
                "persistence did not quiesce"
            )
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure
        if manifest_error is not None:
            failure = RuntimeError(
                "technical workflow manifest persistence failed"
            )
            if tracer_failure is not None:
                _add_failure_note(
                    failure,
                    "workflow trace also failed: "
                    f"{type(tracer_failure).__name__}: {tracer_failure}",
                )
            raise failure from manifest_error
        if tracer_failure is not None:
            raise RuntimeError(
                "technical workflow trace failed: orchestration evidence is incomplete"
            ) from tracer_failure
        return result
    finally:
        if owns_tracer:
            if tracer_close_deferred:
                pass
            elif cleanup_quiesced:
                if not tracer_closed:
                    _close_tracer_capture(tracer)
            else:
                _defer_owned_tracer_close(
                    ctx,
                    tracer,
                    lingering_cleanup_tasks,
                    timeout=cleanup_timeout,
                )


def _add_failure_note(error: BaseException, note: str) -> None:
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)


def _merge_failure(
    primary: BaseException | None,
    secondary: BaseException | None,
    *,
    note_prefix: str,
) -> BaseException | None:
    if primary is None:
        return secondary
    if secondary is not None:
        _add_failure_note(
            primary,
            f"{note_prefix}: {type(secondary).__name__}: {secondary}",
        )
    return primary


def _close_tracer_capture(tracer: TracePort) -> BaseException | None:
    close = getattr(tracer, "close", None)
    if not callable(close):
        return None
    try:
        close()
    except BaseException as exc:
        return exc
    return None


def _inspect_tracer(
    tracer: TracePort | None,
) -> tuple[str | None, int, BaseException | None]:
    if tracer is None:
        return None, 0, None
    try:
        raw_write_error = getattr(tracer, "write_error", None)
        write_error = str(raw_write_error) if raw_write_error else None
        dropped_steps = int(getattr(tracer, "dropped_steps", 0) or 0)
        if dropped_steps < 0:
            raise ValueError("dropped_steps must be non-negative")
    except BaseException as exc:
        return (
            None,
            0,
            RuntimeError(
                "workflow tracer diagnostics could not be inspected: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    return write_error, dropped_steps, None


def _sticky_tracer_failure(
    write_error: str | None,
    dropped_steps: int,
) -> BaseException | None:
    if not write_error:
        return None
    return OSError(
        f"trajectory write failed after dropping {dropped_steps} step(s): "
        f"{write_error}"
    )


def _positive_cleanup_timeout(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("cleanup_timeout must be a finite positive number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup_timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("cleanup_timeout must be a finite positive number")
    return timeout


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _wait_for_context_cleanup(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> bool:
    """Wait through one stable empty turn for all context-owned cleanup."""
    deadline = asyncio.get_running_loop().time() + timeout
    saw_empty = False
    while True:
        pending = set(ctx.pending_cleanup_tasks)
        if not pending:
            if saw_empty:
                return True
            saw_empty = True
            await asyncio.sleep(0)
            continue
        saw_empty = False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        _done, still_pending = await asyncio.wait(pending, timeout=remaining)
        if still_pending:
            return False


def _session_environments(ctx: WorkflowContext) -> tuple[Any, ...]:
    environments: list[Any] = []
    seen: set[int] = set()
    for session in ctx.sessions:
        environment = getattr(session, "env", None)
        if environment is None:
            tool_execution = getattr(session, "tool_execution", None)
            environment = getattr(tool_execution, "environment", None)
        if environment is None or id(environment) in seen:
            continue
        seen.add(id(environment))
        environments.append(environment)
    return tuple(environments)


def _session_persistence_succeeded(ctx: WorkflowContext) -> bool:
    return all(
        not getattr(session, "persistence_errors", ())
        for session in ctx.sessions
    )


async def _abort_session_environments(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    """Synchronously revoke session environments, then bound their abort hooks."""
    abort_tasks: set[asyncio.Future[Any]] = set()
    succeeded = True
    for environment in _session_environments(ctx):
        try:
            setattr(environment, "_aborted", True)
        except Exception:
            succeeded = False
        abort = getattr(environment, "abort", None)
        if not callable(abort):
            continue
        try:
            outcome = abort()
        except Exception:
            succeeded = False
            continue
        if not inspect.isawaitable(outcome):
            continue
        try:
            abort_tasks.add(asyncio.ensure_future(outcome))
        except Exception:
            succeeded = False
            close = getattr(outcome, "close", None)
            if callable(close):
                close()

    for task in ctx.pending_cleanup_tasks:
        task.cancel()

    if not abort_tasks:
        return True, succeeded, ()
    done, pending = await asyncio.wait(abort_tasks, timeout=timeout)
    for task in done:
        try:
            task.result()
        except BaseException:
            succeeded = False
    for task in pending:
        task.cancel()
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    await isolate_tasks_from_shutdown(pending, timeout=timeout)
    abort_quiesced = not pending
    return abort_quiesced, succeeded and abort_quiesced, tuple(pending)


async def _quiesce_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    if await _wait_for_context_cleanup(ctx, timeout=timeout):
        return True, _session_persistence_succeeded(ctx), ()
    abort_quiesced, abort_succeeded, lingering_abort_tasks = (
        await _abort_session_environments(
            ctx,
            timeout=timeout,
        )
    )
    cleanup_quiesced = await _wait_for_context_cleanup(ctx, timeout=timeout)
    all_quiesced = abort_quiesced and cleanup_quiesced
    return (
        all_quiesced,
        (
            abort_succeeded
            and all_quiesced
            and _session_persistence_succeeded(ctx)
        ),
        lingering_abort_tasks,
    )


async def _quiesce_and_finalize_workflow_context(
    ctx: WorkflowContext,
    *,
    timeout: float,
) -> tuple[bool, bool, tuple[asyncio.Future[Any], ...]]:
    """Quiesce execution, enqueue final snapshots, then quiesce persistence."""
    quiesced, succeeded, lingering = await _quiesce_workflow_context(
        ctx,
        timeout=timeout,
    )
    if not quiesced:
        return quiesced, succeeded, lingering

    enqueue_succeeded = True
    for session in ctx.sessions:
        enqueue = getattr(session, "enqueue_auto_save", None)
        if not callable(enqueue):
            continue
        try:
            enqueue()
        except Exception:
            enqueue_succeeded = False

    final_quiesced, final_succeeded, final_lingering = (
        await _quiesce_workflow_context(
            ctx,
            timeout=timeout,
        )
    )
    return (
        quiesced and final_quiesced,
        succeeded and enqueue_succeeded and final_succeeded,
        (*lingering, *final_lingering),
    )


async def _await_cleanup_despite_cancellation(
    cleanup_task: asyncio.Task[tuple[bool, bool, tuple[asyncio.Future[Any], ...]]],
) -> tuple[
    tuple[bool, bool, tuple[asyncio.Future[Any], ...]],
    asyncio.CancelledError | None,
]:
    """Keep one owned cleanup task alive through repeated caller cancellation."""
    first_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(cleanup_task), first_cancellation
        except asyncio.CancelledError as exc:
            if cleanup_task.done() and cleanup_task.cancelled():
                raise
            if first_cancellation is None:
                first_cancellation = exc


def _workflow_manifest_owner_done(task: asyncio.Task[Any]) -> None:
    _WORKFLOW_MANIFEST_OWNER_TASKS.discard(task)
    _consume_task_result(task)


async def _persist_workflow_manifest_owned(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    timeout: float,
) -> tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]:
    """Persist one frozen manifest through an owned, bounded worker task."""
    manifest = _workflow_manifest_payload(
        name=name,
        args=args,
        ctx=ctx,
        tracer=tracer,
        tracer_failure=tracer_failure,
        tracer_write_error=tracer_write_error,
        tracer_dropped_steps=tracer_dropped_steps,
    )
    subscriber = AutoSaveSubscriber(
        lambda: _write_workflow_manifest(
            save_dir,
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            manifest=manifest,
        )
    )
    owner = subscriber.enqueue()
    if owner is None:
        return True, subscriber.last_error, ()

    _WORKFLOW_MANIFEST_OWNER_TASKS.add(owner)
    owner.add_done_callback(_workflow_manifest_owner_done)
    pending: set[asyncio.Task[Any]] = {owner}
    _done, pending = await asyncio.wait(pending, timeout=timeout)
    if pending:
        for task in pending:
            task.cancel()
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    await isolate_tasks_from_shutdown(pending, timeout=timeout)
    return not pending, subscriber.last_error, tuple(pending)


async def _await_manifest_despite_cancellation(
    manifest_task: asyncio.Task[
        tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]
    ],
) -> tuple[
    tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]],
    asyncio.CancelledError | None,
]:
    """Keep manifest ownership alive through repeated caller cancellation."""
    first_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(manifest_task), first_cancellation
        except asyncio.CancelledError as exc:
            if manifest_task.done() and manifest_task.cancelled():
                raise
            if first_cancellation is None:
                first_cancellation = exc


async def _wait_for_late_quiescence(
    ctx: WorkflowContext,
    extra_tasks: Sequence[asyncio.Future[Any]],
    *,
    timeout: float,
) -> bool:
    """Wait through loop-shutdown cancellation after the boundary reported."""
    extras = set(extra_tasks)
    deadline = asyncio.get_running_loop().time() + timeout
    saw_empty = False
    while True:
        pending = {task for task in extras if not task.done()}
        pending.update(ctx.pending_cleanup_tasks)
        current = asyncio.current_task()
        if current is not None:
            pending.discard(current)
        if not pending:
            if saw_empty:
                return True
            saw_empty = True
            await asyncio.sleep(0)
            continue
        saw_empty = False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await isolate_tasks_from_shutdown(pending, timeout=timeout)
            return False
        waiter = asyncio.create_task(asyncio.wait(pending, timeout=remaining))
        while True:
            try:
                await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    break
                continue
        _done, still_pending = waiter.result()
        if still_pending and asyncio.get_running_loop().time() >= deadline:
            await isolate_tasks_from_shutdown(still_pending, timeout=timeout)
            return False
        extras.update(still_pending)


async def _close_tracer_after_late_cleanup(
    ctx: WorkflowContext,
    tracer: TracePort,
    extra_tasks: Sequence[asyncio.Future[Any]],
    *,
    timeout: float,
) -> None:
    quiesced = False
    try:
        quiesced = await _wait_for_late_quiescence(
            ctx,
            extra_tasks,
            timeout=timeout,
        )
    finally:
        if not quiesced:
            _LATE_TRACER_FAILURES.append(
                TimeoutError(
                    "late workflow tracer dependencies did not quiesce before "
                    "their final deadline"
                )
            )
        try:
            tracer.close()
        except BaseException as exc:
            _LATE_TRACER_FAILURES.append(exc)
            raise


def _late_tracer_owner_done(task: asyncio.Task[Any]) -> None:
    _LATE_TRACER_OWNER_TASKS.discard(task)
    _consume_task_result(task)


def _defer_owned_tracer_close(
    ctx: WorkflowContext,
    tracer: TracePort,
    extra_tasks: Sequence[asyncio.Future[Any]],
    *,
    timeout: float,
) -> None:
    late_timeout = min(2.0, max(0.1, timeout))
    owner = asyncio.create_task(
        _close_tracer_after_late_cleanup(
            ctx,
            tracer,
            extra_tasks,
            timeout=late_timeout,
        )
    )
    _LATE_TRACER_OWNER_TASKS.add(owner)
    owner.add_done_callback(_late_tracer_owner_done)


def _workflow_manifest_payload(
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
) -> dict[str, Any]:
    """Freeze all event-loop-owned values used by the workflow manifest."""
    return copy.deepcopy({
        "workflow": name,
        "args": args,
        "sessions": len(ctx.sessions),
        "tokens_spent": ctx.budget.spent(),
        "budget_total": ctx.budget.total,
        "trace_enabled": tracer is not None,
        "tracer_write_error": tracer_write_error,
        "tracer_dropped_steps": tracer_dropped_steps,
        "tracer_failure": (
            f"{type(tracer_failure).__name__}: {tracer_failure}"
            if tracer_failure is not None
            else None
        ),
    })


def _write_workflow_manifest(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Write ``<save_dir>/workflow.json`` summarising the run.

    Ties the run folder's per-role ``<seq>_<role>.json`` transcripts to the
    workflow that produced them, mirroring the chat ``team.json`` manifest.
    """
    if manifest is None:
        manifest = _workflow_manifest_payload(
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
        )
    SessionStore().save_manifest(
        os.path.join(save_dir, WORKFLOW_MANIFEST_FILENAME), manifest
    )


def discover_workflows(directory: str) -> Registry:
    """Load every ``@workflow``-decorated function under ``directory``.

    Imports each top-level ``*.py`` file (skipping dunder/private names) via
    importlib and registers every function carrying a ``__workflow_spec__``. A
    missing directory yields an empty registry.
    """
    registry = Registry()
    directory = os.path.abspath(directory)
    try:
        inspected = os.lstat(directory)
    except FileNotFoundError:
        return registry
    if not stat.S_ISDIR(inspected.st_mode):
        raise ValueError(f"workflow path is not a real directory: {directory}")

    filenames: list[str] = []
    with os.scandir(directory) as entries:
        scanned = 0
        for entry in entries:
            scanned += 1
            if scanned > MAX_WORKFLOW_DIRECTORY_ENTRIES:
                raise ValueError(
                    "workflow directory entries exceed limit of "
                    f"{MAX_WORKFLOW_DIRECTORY_ENTRIES}"
                )
            filename = entry.name
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(f"workflow source is not a regular file: {entry.path}")
            if entry.stat(follow_symlinks=False).st_size > MAX_WORKFLOW_SOURCE_BYTES:
                raise ValueError(
                    "workflow source exceeds "
                    f"{MAX_WORKFLOW_SOURCE_BYTES}-byte limit: {entry.path}"
                )
            filenames.append(filename)
            if len(filenames) > MAX_WORKFLOW_FILES:
                raise ValueError(
                    f"workflow files exceed limit of {MAX_WORKFLOW_FILES}"
                )

    for filename in sorted(filenames):
        path = os.path.join(directory, filename)
        for spec in _load_specs_from_file(path):
            registry.register(spec)
    return registry


def _load_specs_from_file(path: str) -> list[WorkflowSpec]:
    """Import a single python file and collect its workflow specs."""
    module_name = f"_opencollab_workflow_{uuid.uuid4().hex}"
    source = read_regular_text(path, max_bytes=MAX_WORKFLOW_SOURCE_BYTES)
    module = types.ModuleType(module_name)
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)

    # Dedupe by spec identity: a decorated function bound under more than one
    # module-level name (an alias or a re-export) carries the SAME spec object
    # under each name. Collecting both would register the same name twice and
    # abort discovery of the whole directory, so keep one entry per spec.
    found: list[WorkflowSpec] = []
    seen: set[int] = set()
    for value in vars(module).values():
        wf_spec = getattr(value, "__workflow_spec__", None)
        if isinstance(wf_spec, WorkflowSpec) and id(wf_spec) not in seen:
            seen.add(id(wf_spec))
            found.append(wf_spec)
    return found


__all__ = [
    "WORKFLOW_AGENT_PROMPT",
    "WorkflowSessionFactory",
    "build_session",
    "build_workflow_context",
    "discover_workflows",
    "run_workflow",
]
