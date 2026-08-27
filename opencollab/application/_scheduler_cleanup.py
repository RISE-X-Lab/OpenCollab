"""Bounded scheduler teardown and persistence finalization."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import Any

from opencollab.application._scheduler_constants import DEFAULT_SCHEDULER_CLEANUP_TIMEOUT
from opencollab.application.async_timeout import (
    await_owned_operation,
    cancel_tasks_and_wait,
    consume_task_result,
)
from opencollab.application.session_lifecycle import close_session_resources
from opencollab.domain.pending import PendingRowError, RowStatus

logger = logging.getLogger(__name__)


def _unique_task_owners(*groups: Any) -> list[tuple[int, asyncio.Task[Any]]]:
    owners: list[tuple[int, asyncio.Task[Any]]] = []
    seen: set[asyncio.Task[Any]] = set()
    for group in groups:
        for aid, task in group:
            if task not in seen:
                seen.add(task)
                owners.append((aid, task))
    return owners


class SchedulerCleanupMixin:
    async def cleanup(
        self,
        *,
        cleanup_timeout: float = DEFAULT_SCHEDULER_CLEANUP_TIMEOUT,
    ) -> None:
        """Stop scheduler-owned work and persist one terminal snapshot."""
        timeout = self._validate_cleanup_timeout(cleanup_timeout)
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_impl(timeout=timeout))
        task = self._cleanup_task
        try:
            await await_owned_operation(task, propagate_cancellation=True)
        except BaseException:
            # Share one in-flight teardown across concurrent callers, but do not
            # permanently memoize a transient failure. Caller cancellation alone
            # is not a retry signal: await_owned_operation keeps the owner alive
            # and may re-raise cancellation after that owner succeeded.
            failed = task.done() and (
                task.cancelled() or task.exception() is not None
            )
            if failed and self._cleanup_task is task:
                self._cleanup_task = None
            raise

    @staticmethod
    def _validate_cleanup_timeout(value: object) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("cleanup_timeout must be a finite number greater than zero") from exc
        if isinstance(value, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("cleanup_timeout must be a finite number greater than zero")
        return timeout

    async def _cleanup_impl(self, *, timeout: float) -> None:
        self._shutting_down = True
        failures: list[str] = []
        persistence_sessions = tuple(self._sessions.values())
        startup_aids = set(self._startup_tasks)
        origins = {**self._startup_origin, **self._spawn_origin}
        interrupted_deliveries = set(self._message_delivery_tasks)
        public_run_tasks = _unique_task_owners(
            ((aid, task) for task, aid in self._active_run_tasks.items()),
        )
        execution_tasks = _unique_task_owners(
            self._tasks.items(),
            self._startup_tasks.items(),
        )
        delivery_tasks = _unique_task_owners(
            self._message_delivery_tasks.items(),
            public_run_tasks,
        )
        tracked_tasks = _unique_task_owners(execution_tasks, delivery_tasks)
        running_execution = {
            (aid, task) for aid, task in execution_tasks if not task.done()
        }
        running_public_turns = {
            (aid, task) for aid, task in public_run_tasks if not task.done()
        }
        all_tasks = {task for _aid, task in tracked_tasks}
        pending = await cancel_tasks_and_wait(all_tasks, timeout=timeout)
        for task in all_tasks - pending:
            consume_task_result(task)
        for task in pending:
            task.add_done_callback(consume_task_result)

        pending_aids = {aid for aid, task in running_execution if task in pending}
        environments_aborted = await self._abort_session_environments(
            pending_aids,
            timeout=timeout,
        )
        pending = {task for task in pending if not task.done()}
        if pending:
            failures.append("execution tasks did not quiesce")
        if interrupted_deliveries:
            failures.append("message delivery was interrupted")
        if not environments_aborted:
            failures.append("session environment abort failed or timed out")

        for aid, task in running_execution:
            if task.cancelled() or task in pending or (task.done() and task.exception() is not None):
                self._finalize_cleanup_failure(aid, origin_override=origins.get(aid))
        for aid, _task in running_public_turns:
            self._finalize_cleanup_failure(aid)
        for aid in startup_aids:
            self._finalize_cleanup_failure(aid, origin_override=origins.get(aid))
            self.table.entries.pop(aid, None)
            self._sessions.pop(aid, None)
            self._locks.pop(aid, None)
            self._run_locks.pop(aid, None)
            self._message_inbox.pop(aid, None)

        self._startup_tasks.clear()
        self._startup_envs.clear()
        self._startup_origin.clear()
        self._message_delivery_tasks.clear()
        self._active_run_tasks.clear()
        self._turn_lease.clear()
        self._lease_baseline.clear()
        self._inflight.clear()
        self._inflight_key_of.clear()
        self._delivery_committed.clear()
        self._tasks.clear()
        self._turn_started_at.clear()
        self._turn_cancel_events.clear()

        persistence_quiesced = await self._wait_for_session_persistence(
            persistence_sessions,
            timeout=timeout,
        )
        if persistence_quiesced:
            self._autosave_all_sessions()
            self._write_manifest()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=max(0.1, timeout),
            )
        persistence_errors = self._session_persistence_errors(persistence_sessions)
        if not persistence_quiesced:
            failures.append("session-owned tasks did not quiesce")
        if persistence_errors:
            failures.append("session persistence failed")

        session_resources_closed = await close_session_resources(
            self._persistence_sessions(persistence_sessions),
            timeout=timeout,
        )
        if not session_resources_closed:
            failures.append("session resource close failed or timed out")

        lifecycle_errors: list[BaseException] = []
        lifecycle_quiesced = True
        for description, resource in self._lifecycle_resources:
            if getattr(resource, "cleanup_quiesced", False) is True:
                continue
            lifecycle_quiesced = False
            failures.append(f"{description} did not quiesce")
            error = getattr(resource, "cleanup_error", None)
            if isinstance(error, BaseException):
                lifecycle_errors.append(error)

        release_safe = (
            not pending
            and environments_aborted
            and persistence_quiesced
            and session_resources_closed
            and lifecycle_quiesced
        )
        worktrees_released = release_safe and await self._release_worktree_pool_bounded(timeout=timeout)
        if not worktrees_released:
            failures.append(
                "worktree pool release failed or timed out"
                if release_safe
                else "worktree pool release skipped because owned work did not quiesce"
            )
        if failures:
            failure = RuntimeError("technical scheduler cleanup failed: " + "; ".join(failures))
            if persistence_errors:
                raise failure from persistence_errors[0]
            if lifecycle_errors:
                raise failure from lifecycle_errors[0]
            raise failure

    def _persistence_sessions(self, initial: tuple[Any, ...]) -> tuple[Any, ...]:
        sessions: list[Any] = []
        seen: set[int] = set()
        for session in (*initial, *self._sessions.values()):
            if id(session) not in seen:
                seen.add(id(session))
                sessions.append(session)
        return tuple(sessions)

    def _pending_session_persistence_tasks(
        self,
        sessions: tuple[Any, ...],
    ) -> set[asyncio.Task[Any]]:
        current = asyncio.current_task()
        pending = {
            task
            for session in self._persistence_sessions(sessions)
            for task in getattr(session, "pending_cleanup_tasks", ())
            if isinstance(task, asyncio.Task) and not task.done() and task is not current
        }
        for subscriber in self._fallback_autosavers.values():
            pending.update(subscriber.pending_tasks)
        if self._manifest_subscriber is not None:
            pending.update(self._manifest_subscriber.pending_tasks)
        return pending

    async def _wait_for_session_persistence(
        self,
        sessions: tuple[Any, ...],
        *,
        timeout: float,
    ) -> bool:
        pending = self._pending_session_persistence_tasks(sessions)
        if not pending:
            return True
        _done, pending = await asyncio.wait(pending, timeout=timeout)
        if pending:
            for task in pending:
                task.cancel()
                task.add_done_callback(consume_task_result)
            return False
        for task in _done:
            consume_task_result(task)
        return True

    def _session_persistence_errors(
        self,
        sessions: tuple[Any, ...],
    ) -> tuple[Exception, ...]:
        errors = list(self._scheduler_persistence_errors)
        for session in self._persistence_sessions(sessions):
            errors.extend(
                error
                for error in getattr(session, "persistence_errors", ())
                if isinstance(error, Exception)
            )
        errors.extend(
            subscriber.last_error
            for subscriber in self._fallback_autosavers.values()
            if subscriber.last_error is not None
        )
        if self._manifest_subscriber is not None and self._manifest_subscriber.last_error is not None:
            errors.append(self._manifest_subscriber.last_error)
        return tuple(errors)

    def _finalize_cleanup_failure(
        self,
        aid: int,
        *,
        origin_override: tuple[int, str] | None = None,
    ) -> None:
        reason = "Error: scheduler cleanup cancelled delegated work"
        self._release_leases(aid)
        if aid in self._delivery_committed:
            return
        origin = self._spawn_origin.pop(aid, None) or self._startup_origin.pop(aid, None) or origin_override
        if origin is not None:
            parent_aid, tool_call_id = origin
            parent = self.table.get(parent_aid)
            if parent is not None and tool_call_id in parent.state.pending_events.rows:
                try:
                    parent.state.pending_events.fill(
                        tool_call_id,
                        result=reason,
                        status=RowStatus.FAILED,
                        error=reason,
                    )
                except PendingRowError:
                    logger.debug("cleanup could not fail pending row %s on aid %s", tool_call_id, parent_aid)
        scb = self.table.get(aid)
        if scb is not None:
            scb.state.cancel(reason)
            scb.result = reason

    async def _abort_session_environments(self, aids: set[int], *, timeout: float) -> bool:
        environments: list[tuple[int, Any]] = []
        seen: set[int] = set()
        for aid in aids:
            session = self._sessions.get(aid)
            candidates = [self._startup_envs.get(aid)]
            if session is not None:
                candidates.extend(
                    [
                        getattr(session, "env", None),
                        getattr(getattr(session, "tool_execution", None), "environment", None),
                    ]
                )
            for environment in candidates:
                if environment is not None and id(environment) not in seen:
                    seen.add(id(environment))
                    environments.append((aid, environment))

        succeeded = True
        abort_tasks: set[asyncio.Task[Any]] = set()
        for aid, environment in environments:
            try:
                revoke = getattr(environment, "revoke", None)
                if callable(revoke):
                    revoke()
            except BaseException as exc:
                succeeded = False
                logger.error("session environment revoke failed for aid %s: %s", aid, exc)
            abort = getattr(environment, "abort", None)
            if not callable(abort):
                continue
            try:
                result = abort()
                if inspect.isawaitable(result):
                    abort_tasks.add(asyncio.ensure_future(result))
            except BaseException as exc:
                succeeded = False
                logger.error("session environment abort failed for aid %s: %s", aid, exc)
        if abort_tasks:
            done, pending = await asyncio.wait(abort_tasks, timeout=timeout)
            for task in done:
                try:
                    task.result()
                except BaseException as exc:
                    succeeded = False
                    logger.error("session environment abort task failed: %s", exc)
            for task in pending:
                task.cancel()
                task.add_done_callback(consume_task_result)
            if pending:
                succeeded = False
                logger.error("%s session environment abort task(s) missed the cleanup deadline", len(pending))
        return succeeded

    async def _release_worktree_pool_bounded(self, *, timeout: float) -> bool:
        try:
            result = self._worktree_pool.release()
            if not inspect.isawaitable(result):
                return True
            task = asyncio.ensure_future(result)
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                task.cancel()
                task.add_done_callback(consume_task_result)
                return False
            task.result()
            return bool(done)
        except BaseException as exc:
            logger.error("worktree pool release failed: %s", exc)
            return False
