"""Isolated candidate execution for :class:`WorkflowContext`."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError, await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.workflow_budget import _BudgetLease


def _candidate_budget_total(budget: int | None, limit_mode: str = "environment") -> int | None:
    if (
        limit_mode == "environment"
        and os.environ.get("OPENCOLLAB_UNBOUNDED_LIMITS", "").strip().lower() in {"1", "true"}
    ):
        return None
    return budget


async def _wait_for_candidate_cleanup(lease: _BudgetLease) -> None:
    """Drain this candidate's turn tasks and session-owned cleanup."""
    current = asyncio.current_task()
    saw_empty = False
    while True:
        owned = {
            *(lease.pending_tasks or ()),
            *(
                task
                for session in lease.sessions
                for task in getattr(session, "pending_cleanup_tasks", ())
                if isinstance(task, asyncio.Future)
            ),
        }
        pending = {task for task in owned if not task.done() and task is not current}
        if not pending:
            if saw_empty:
                return
            saw_empty = True
        else:
            saw_empty = False
            await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
        # Finishing turn tasks and callbacks can register new session cleanup.
        await asyncio.sleep(0)


@dataclass(frozen=True, slots=True)
class CandidateRun:
    """One isolated agent result with its complete candidate evidence."""

    label: str
    output: str | dict[str, Any] | None
    diff: str
    test_records: tuple[dict[str, Any], ...]
    verified_targets: tuple[str, ...]
    lifecycle_errors: tuple[str, ...] = ()
    source_revision: str | None = None


class CandidateCaptureError(RuntimeError):
    """A candidate worktree survived but its diff could not be captured."""


class CandidateWorkspaceTrackingError(RuntimeError):
    """The source worktree changed while a candidate was executing."""


class _CandidateLeaseTreeProbe:
    """Expose one candidate lease through the working-tree probe contract."""

    def __init__(self, lease: Any) -> None:
        self._lease = lease

    async def changed(self) -> bool:
        return bool((await self._lease.diff()).strip())

    async def changed_excluding(self, paths: Sequence[str]) -> bool:
        if not paths:
            return await self.changed()
        return bool((await self._lease.diff(exclude_paths=paths)).strip())

    async def diff(self) -> str:
        return await self._lease.diff()

    async def diff_excluding(self, paths: Sequence[str]) -> str:
        return await self._lease.diff(exclude_paths=paths)


def _accepts_environment(method: Any) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == "environment" and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        for parameter in parameters
    )


class _CandidateWorkflowSessionFactory:
    """Bind every child workflow session to one candidate environment."""

    def __init__(self, factory: Any, environment: Any) -> None:
        self._factory = factory
        self._environment = environment
        self._isolated_environments: list[tuple[Any, Any]] = []
        self._isolated_sessions: list[Any] = []

    @property
    def environment_revoked(self) -> bool:
        return bool(getattr(self._environment, "revoked", False))

    @property
    def has_pending_isolated_cleanup(self) -> bool:
        return bool(self._isolated_environments)

    async def acquire_isolated_env(
        self, *, label: str | None = None, environment: Any | None = None,
    ) -> Any:
        owner = self._environment if environment is None else environment
        acquire = getattr(self._factory, "acquire_isolated_env", None)
        release = getattr(self._factory, "release_isolated_envs", None)
        if not callable(acquire) or not callable(release) or not (
            _accepts_environment(acquire) and _accepts_environment(release)
        ):
            raise TypeError("candidate isolation requires environment-aware acquisition and scoped release")
        isolated = await acquire(label=label, environment=owner)
        self._isolated_environments.append((owner, isolated))
        return isolated

    def build_workflow_session(self, **kwargs: Any) -> Any:
        environment = kwargs.get("env")
        if environment is None:
            environment = self._environment
        session = self._factory.build_workflow_session(**{**kwargs, "env": environment})
        if any(
            owner is self._environment and environment is isolated
            for owner, isolated in self._isolated_environments
        ):
            self._isolated_sessions.append(session)
        return session

    async def release_isolated_envs(self, *, environment: Any | None = None) -> None:
        owner = self._environment if environment is None else environment
        if not any(source is owner for source, _isolated in self._isolated_environments):
            return
        errors: list[Exception] = []
        sessions = self._isolated_sessions if owner is self._environment else []
        for session in sessions:
            close = getattr(session, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:
                    errors.append(exc)
        active = any(
            not task.done()
            for session in sessions
            for task in getattr(session, "pending_cleanup_tasks", ())
            if isinstance(task, asyncio.Future)
        )
        if active:
            errors.append(RuntimeError("isolated session retains active cleanup"))
        else:
            release = getattr(self._factory, "release_isolated_envs", None)
            try:
                if not callable(release) or not _accepts_environment(release):
                    raise TypeError("candidate isolation requires environment-scoped release")
                await release(environment=owner)
            except Exception as exc:
                errors.append(exc)
            else:
                self._isolated_environments = [
                    pair for pair in self._isolated_environments if pair[0] is not owner
                ]
                if owner is self._environment:
                    self._isolated_sessions.clear()
        if errors:
            detail = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            raise RuntimeError(f"candidate isolation cleanup failed: {detail}") from errors[0]

    async def execute_verification(
        self,
        tool: Any,
        params: Mapping[str, object],
        *,
        environment: Any | None = None,
    ) -> str:
        resolved = self._environment if environment is None else environment
        if bool(getattr(resolved, "revoked", False)):
            raise RuntimeError("candidate verification environment is unavailable")
        return await self._factory.execute_verification(tool, params, environment=resolved)


def _verification_evidence(
    tools: list[Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    records: list[dict[str, Any]] = []
    targets: set[str] = set()
    for tool in tools:
        targets.update(str(item) for item in getattr(tool, "verified_targets", ()) if str(item))
        for record in getattr(tool, "verification_records", ()):
            if isinstance(record, dict):
                records.append(dict(record))
    return tuple(records), tuple(sorted(targets))


def _candidate_verification_tools(tools: Sequence[Any]) -> list[Any]:
    """Keep native execution settings and fork candidate-owned observations."""
    scope: dict[object, Any] = {}
    selected = []
    for tool in tools:
        fork = getattr(tool, "fork_verification_scope", None)
        selected.append(fork(scope) if callable(fork) else tool)
    return selected


class WorkflowCandidatesMixin:
    """Runs agent sessions in candidate leases and adopts a selected diff."""

    async def _run_owned_candidate_call(
        self, operation: Callable[[], Awaitable[CandidateRun]],
    ) -> CandidateRun:
        """Own acquisition, execution, capture, and cleanup as one call."""
        current = asyncio.current_task()
        already_owned = current in self._active_call_tasks
        if current is not None:
            self._active_call_tasks.add(current)
        try:
            return await operation()
        finally:
            if current is not None and not already_owned:
                self._active_call_tasks.discard(current)

    async def _candidate_source_state(self) -> tuple[str | None, str]:
        read_revision = getattr(self._candidate_workspace, "source_revision", None)
        revision = await read_revision() if callable(read_revision) else None
        return revision, await self._candidate_workspace.source_diff()

    async def candidate_agent(
        self,
        prompt: str,
        *,
        label: str,
        tools: Sequence[Any] | None = None,
        budget: int | None = None,
        timeout: float | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
    ) -> CandidateRun:
        if self._candidate_workspace is None:
            raise RuntimeError("candidate workspaces are not available")
        timeout = self._normalize_timeout(timeout)
        selected_tools = _candidate_verification_tools(tools or ())

        async def run() -> CandidateRun:
            source_before = await self._candidate_source_state()
            lease = await self._candidate_workspace.acquire(label)
            budget_lease = None
            token = None
            candidate: CandidateRun | None = None
            failure: BaseException | None = None
            preserve_lease = False
            try:
                budget_lease = await self._acquire_budget_lease(
                    budget,
                    over_budget_ok=False,
                )
                token = self._active_budget_lease.set(budget_lease)
                session = self._factory.build_workflow_session(
                    prompt=prompt,
                    budget=self._capped_session_budget(budget),
                    tools=selected_tools,
                    isolation=False,
                    label=label,
                    tool_choice=tool_choice,
                    thinking=thinking,
                    env=lease.environment,
                )
                self._track_session(session)
                try:
                    output = await self._run_session_turn(
                        session,
                        prompt,
                        deadline=self._timeout_deadline(timeout),
                    )
                except CallerTimeoutError:
                    output = None
                    await self.log(
                        f"candidate agent timed out ({label}) after {timeout}s"
                    )
                except Exception as exc:  # noqa: BLE001 - preserve candidate edits
                    output = None
                    self._record_agent_failure(label, exc)
                    await self.log(f"candidate agent failed ({label}): {exc}")
                await _wait_for_candidate_cleanup(budget_lease)
                try:
                    diff = await lease.diff()
                except Exception as exc:
                    failure = CandidateCaptureError(
                        f"candidate diff capture failed ({label}); "
                        f"worktree preserved at {lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                try:
                    source_after = await self._candidate_source_state()
                except asyncio.CancelledError:
                    preserve_lease = True
                    raise
                except Exception as exc:
                    failure = CandidateWorkspaceTrackingError(
                        f"candidate patch was captured, but could not verify source "
                        f"worktree state after candidate {label}; worktree preserved at "
                        f"{lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                if source_after != source_before:
                    failure = CandidateWorkspaceTrackingError(
                        f"source worktree changed during candidate {label}. "
                        f"worktree preserved at {lease.candidate_workspace}. "
                        f"original source tree {getattr(lease, 'base_revision', 'unavailable')}"
                    )
                    raise failure
                records, targets = _verification_evidence(selected_tools)
                candidate = CandidateRun(
                    label=label,
                    output=output,
                    diff=diff,
                    test_records=records,
                    verified_targets=targets,
                    source_revision=source_before[0],
                )
            except BaseException as exc:
                failure = exc
            finally:
                if token is not None:
                    self._active_budget_lease.reset(token)
                if budget_lease is not None:
                    try:
                        await await_owned_operation(
                            _wait_for_candidate_cleanup(budget_lease),
                            propagate_cancellation=True,
                        )
                    except asyncio.CancelledError as exc:
                        if failure is None:
                            failure = exc
                    self.budget.release(budget_lease)
            if failure is not None:
                if (
                    not preserve_lease
                    and not isinstance(failure, (CandidateCaptureError, CandidateWorkspaceTrackingError))
                ):
                    try:
                        await lease.cleanup()
                    except Exception as cleanup_exc:
                        add_exception_note(
                            failure,
                            "candidate cleanup after failure also failed: "
                            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
                raise failure
            assert candidate is not None
            try:
                await lease.cleanup()
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._record_agent_failure(f"{label}:cleanup", exc)
                await self.log(f"candidate cleanup failed ({label}): {detail}")
                candidate = replace(
                    candidate,
                    lifecycle_errors=(*candidate.lifecycle_errors, detail),
                )
            return candidate

        return await self._run_owned_candidate_call(lambda: self._run_with_concurrency_permit(run))

    async def candidate_workflow(
        self,
        workflow_fn: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        args: dict[str, Any],
        *,
        label: str,
        budget: int | None = None,
    ) -> CandidateRun:
        """Run a complete workflow in one isolated candidate worktree."""
        if self._candidate_workspace is None:
            raise RuntimeError("candidate workspaces are not available")
        if not callable(workflow_fn):
            raise TypeError("candidate workflow must be callable")
        if not isinstance(args, dict):
            raise TypeError("candidate workflow args must be a dict")

        async def run() -> CandidateRun:
            source_before = await self._candidate_source_state()
            lease = await self._candidate_workspace.acquire(label)
            budget_lease = None
            child = None
            candidate: CandidateRun | None = None
            failure: BaseException | None = None
            preserve_lease = False
            try:
                budget_lease = await self._acquire_budget_lease(
                    budget,
                    over_budget_ok=False,
                )
                workspace = getattr(lease.environment, "workspace", None)
                child = type(self)(
                    _CandidateWorkflowSessionFactory(
                        self._factory,
                        lease.environment,
                    ),
                    event_sink=self._event_sink,
                    tracer=self._tracer,
                    max_concurrency=self._max_concurrency,
                    task_concurrency=self._task_concurrency,
                    budget_total=_candidate_budget_total(budget_lease.total, self._limit_mode),
                    limit_mode=self._limit_mode,
                    run_id=self.run_id,
                    tree_probe=_CandidateLeaseTreeProbe(lease),
                    candidate_workspace=None,
                    deadline_monotonic=self._deadline_monotonic,
                    deadline_margin_seconds=self._deadline_margin_seconds,
                    workspace_root=workspace if isinstance(workspace, str) else None,
                )
                # Candidate orchestration consumes no agent slot. Its sessions
                # use the same capacity as every other agent in the run.
                child._semaphore = self._semaphore
                child._task_semaphore = self._task_semaphore
                child._active_task_concurrency_permit = self._active_task_concurrency_permit
                child._budget_escape_state = self._budget_escape_state
                try:
                    output = await workflow_fn(child, dict(args))
                except Exception as exc:  # noqa: BLE001 - preserve candidate edits
                    output = None
                    self._record_agent_failure(label, exc)
                    await self.log(f"candidate workflow failed ({label}): {exc}")
                await child.wait_for_pending_cleanup()
                try:
                    diff = await lease.diff()
                except Exception as exc:
                    failure = CandidateCaptureError(
                        f"candidate workflow diff capture failed ({label}); "
                        f"worktree preserved at {lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                try:
                    source_after = await self._candidate_source_state()
                except asyncio.CancelledError:
                    preserve_lease = True
                    raise
                except Exception as exc:
                    failure = CandidateWorkspaceTrackingError(
                        f"candidate patch was captured, but could not verify source "
                        f"worktree state after candidate workflow {label}; worktree preserved at "
                        f"{lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                if source_after != source_before:
                    failure = CandidateWorkspaceTrackingError(
                        f"source worktree changed during candidate workflow {label}. "
                        f"worktree preserved at {lease.candidate_workspace}. "
                        f"original source tree {getattr(lease, 'base_revision', 'unavailable')}"
                    )
                    raise failure
                candidate = CandidateRun(
                    label=label,
                    output=output,
                    diff=diff,
                    test_records=(),
                    verified_targets=(),
                    source_revision=source_before[0],
                )
            except BaseException as exc:
                failure = exc
            finally:
                try:
                    if child is not None:
                        try:
                            await child.wait_for_pending_cleanup()
                            await child.release_isolated_workspaces()
                        except Exception as exc:
                            detail = f"{type(exc).__name__}: {exc}"
                            preserve_lease = preserve_lease or bool(
                                getattr(child._factory, "has_pending_isolated_cleanup", False)
                            )
                            if preserve_lease:
                                detail += f"; candidate worktree retained at {lease.candidate_workspace}"
                            self._record_agent_failure(f"{label}:cleanup", exc)
                            if failure is not None:
                                add_exception_note(failure, detail)
                            elif candidate is not None:
                                candidate = replace(candidate, lifecycle_errors=(*candidate.lifecycle_errors, detail))
                            else:
                                failure = exc
                        finally:
                            self._sessions.extend(child.sessions)
                            for agent_failure in child.agent_failures:
                                self._agent_failures.append({
                                    **agent_failure,
                                    "label": f"{label}/{agent_failure.get('label', 'agent')}"[:240],
                                })
                    if budget_lease is not None:
                        pending = [task for task in budget_lease.pending_tasks or () if not task.done()]
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                finally:
                    if budget_lease is not None:
                        self.budget.release(budget_lease)
            if failure is not None:
                if (
                    not isinstance(failure, (CandidateCaptureError, CandidateWorkspaceTrackingError))
                    and not preserve_lease
                ):
                    try:
                        await lease.cleanup()
                    except Exception as cleanup_exc:
                        add_exception_note(
                            failure,
                            "candidate cleanup after failure also failed: "
                            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
                raise failure
            assert candidate is not None
            if preserve_lease:
                return candidate
            try:
                await lease.cleanup()
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._record_agent_failure(f"{label}:cleanup", exc)
                await self.log(f"candidate cleanup failed ({label}): {detail}")
                candidate = replace(
                    candidate,
                    lifecycle_errors=(*candidate.lifecycle_errors, detail),
                )
            return candidate

        return await self._run_owned_candidate_call(run)

    async def adopt_candidate(
        self,
        candidate: CandidateRun,
        *,
        preserve_paths: Sequence[str] = (),
    ) -> None:
        if self._candidate_workspace is None:
            raise RuntimeError("candidate workspaces are not available")
        if not isinstance(candidate, CandidateRun):
            raise TypeError("candidate must be a CandidateRun")
        read_revision = getattr(self._candidate_workspace, "source_revision", None)
        if (
            candidate.source_revision is not None
            and callable(read_revision)
            and await read_revision() != candidate.source_revision
        ):
            raise CandidateWorkspaceTrackingError(
                f"source worktree changed before candidate adoption {candidate.label}"
            )
        adopt_run = getattr(self._candidate_workspace, "adopt_run", None)
        if callable(adopt_run):
            # Full-environment backends must retain the chosen candidate's
            # identity even when two candidates have identical file diffs.
            await adopt_run(candidate, preserve_paths)
        else:
            await self._candidate_workspace.adopt(candidate.diff, preserve_paths)


__all__ = [
    "CandidateCaptureError",
    "CandidateRun",
    "CandidateWorkspaceTrackingError",
    "WorkflowCandidatesMixin",
]
