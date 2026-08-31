"""Isolated candidate execution for :class:`WorkflowContext`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError


@dataclass(frozen=True, slots=True)
class CandidateRun:
    """One isolated agent result with its complete candidate evidence."""

    label: str
    output: str | dict[str, Any] | None
    diff: str
    test_records: tuple[dict[str, Any], ...]
    verified_targets: tuple[str, ...]
    lifecycle_errors: tuple[str, ...] = ()


class CandidateCaptureError(RuntimeError):
    """A candidate worktree survived but its diff could not be captured."""


class CandidateWorkspaceTrackingError(RuntimeError):
    """A candidate execution changed the source worktree before adoption."""


class _CandidateLeaseTreeProbe:
    """Expose one candidate lease through the working-tree probe contract."""

    def __init__(self, lease: Any) -> None:
        self._lease = lease

    async def changed(self) -> bool:
        return bool((await self._lease.diff()).strip())

    async def changed_excluding(self, paths: Sequence[str]) -> bool:
        del paths
        return await self.changed()

    async def diff(self) -> str:
        return await self._lease.diff()


class _CandidateWorkflowSessionFactory:
    """Bind every child workflow session to one candidate environment."""

    def __init__(self, factory: Any, environment: Any) -> None:
        self._factory = factory
        self._environment = environment

    def build_workflow_session(self, **kwargs: Any) -> Any:
        return self._factory.build_workflow_session(
            **{
                **kwargs,
                "environment": self._environment,
            }
        )

    async def execute_verification(
        self,
        tool: Any,
        params: Mapping[str, object],
        *,
        environment: Any | None = None,
    ) -> str:
        del environment
        if bool(getattr(self._environment, "revoked", False)):
            raise RuntimeError("candidate verification environment is unavailable")
        return await self._factory.execute_verification(
            tool,
            params,
            environment=self._environment,
        )


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


class WorkflowCandidatesMixin:
    """Runs agent sessions in candidate leases and adopts a selected diff."""

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
        selected_tools = list(tools or ())

        async def run() -> CandidateRun:
            source_before = await self._candidate_workspace.source_diff()
            lease = await self._candidate_workspace.acquire(label)
            budget_lease = None
            token = None
            candidate: CandidateRun | None = None
            failure: BaseException | None = None
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
                    environment=lease.environment,
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
                await self.wait_for_pending_cleanup()
                try:
                    diff = await lease.diff()
                except Exception as exc:
                    failure = CandidateCaptureError(
                        f"candidate diff capture failed ({label}); "
                        f"worktree preserved at {lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                source_after = await self._candidate_workspace.source_diff()
                if source_after != source_before:
                    try:
                        await self._candidate_workspace.restore_source(source_before)
                    except Exception as restore_exc:
                        failure = CandidateWorkspaceTrackingError(
                            f"candidate {label} changed the source worktree and "
                            "source restoration failed"
                        )
                        failure.__cause__ = restore_exc
                        raise failure
                    failure = CandidateWorkspaceTrackingError(
                        f"candidate {label} changed the source worktree before adoption"
                    )
                    raise failure
                records, targets = _verification_evidence(selected_tools)
                candidate = CandidateRun(
                    label=label,
                    output=output,
                    diff=diff,
                    test_records=records,
                    verified_targets=targets,
                )
            except BaseException as exc:
                failure = exc
            finally:
                if token is not None:
                    self._active_budget_lease.reset(token)
                if budget_lease is not None:
                    pending = [
                        task
                        for task in budget_lease.pending_tasks or ()
                        if not task.done()
                    ]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    self.budget.release(budget_lease)
            if failure is not None:
                if not isinstance(failure, CandidateCaptureError):
                    try:
                        await lease.cleanup()
                    except Exception as cleanup_exc:
                        failure.add_note(
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

        return await self._run_with_concurrency_permit(run)

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
            source_before = await self._candidate_workspace.source_diff()
            lease = await self._candidate_workspace.acquire(label)
            budget_lease = None
            child = None
            candidate: CandidateRun | None = None
            failure: BaseException | None = None
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
                    budget_total=budget,
                    tree_probe=_CandidateLeaseTreeProbe(lease),
                    candidate_workspace=None,
                    deadline_monotonic=self._deadline_monotonic,
                    deadline_margin_seconds=self._deadline_margin_seconds,
                    workspace_root=workspace if isinstance(workspace, str) else None,
                )
                try:
                    output = await workflow_fn(child, dict(args))
                except Exception as exc:  # noqa: BLE001 - preserve candidate edits
                    output = None
                    self._record_agent_failure(label, exc)
                    await self.log(f"candidate workflow failed ({label}): {exc}")
                await child.wait_for_pending_cleanup()
                self._sessions.extend(child.sessions)
                for agent_failure in child.agent_failures:
                    self._agent_failures.append(
                        {
                            **agent_failure,
                            "label": (
                                f"{label}/{agent_failure.get('label', 'agent')}"
                            )[:240],
                        }
                    )
                try:
                    diff = await lease.diff()
                except Exception as exc:
                    failure = CandidateCaptureError(
                        f"candidate workflow diff capture failed ({label}); "
                        f"worktree preserved at {lease.candidate_workspace}"
                    )
                    failure.__cause__ = exc
                    raise failure
                source_after = await self._candidate_workspace.source_diff()
                if source_after != source_before:
                    try:
                        await self._candidate_workspace.restore_source(source_before)
                    except Exception as restore_exc:
                        failure = CandidateWorkspaceTrackingError(
                            f"candidate workflow {label} changed the source worktree "
                            "and source restoration failed"
                        )
                        failure.__cause__ = restore_exc
                        raise failure
                    failure = CandidateWorkspaceTrackingError(
                        f"candidate workflow {label} changed the source worktree "
                        "before adoption"
                    )
                    raise failure
                candidate = CandidateRun(
                    label=label,
                    output=output,
                    diff=diff,
                    test_records=(),
                    verified_targets=(),
                )
            except BaseException as exc:
                failure = exc
            finally:
                if child is not None:
                    await child.wait_for_pending_cleanup()
                if budget_lease is not None:
                    pending = [
                        task
                        for task in budget_lease.pending_tasks or ()
                        if not task.done()
                    ]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    self.budget.release(budget_lease)
            if failure is not None:
                if not isinstance(failure, CandidateCaptureError):
                    try:
                        await lease.cleanup()
                    except Exception as cleanup_exc:
                        failure.add_note(
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

        return await self._run_with_concurrency_permit(run)

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
        await self._candidate_workspace.adopt(candidate.diff, preserve_paths)


__all__ = [
    "CandidateCaptureError",
    "CandidateRun",
    "CandidateWorkspaceTrackingError",
    "WorkflowCandidatesMixin",
]
