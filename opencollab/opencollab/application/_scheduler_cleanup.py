"""Bounded scheduler teardown and persistence finalization."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import Any

from opencollab.application._scheduler_constants import (
    DEFAULT_SCHEDULER_CLEANUP_TIMEOUT,
    MAX_FORCED_CLEANUP_TIMEOUT,
)
from opencollab.application.async_timeout import force_task_terminal
from opencollab.domain.pending import PendingRowError, RowStatus

logger = logging.getLogger(__name__)
Scheduler: Any = None


class SchedulerCleanupMixin:
    async def cleanup(
        self,
        *,
        cleanup_timeout: float = DEFAULT_SCHEDULER_CLEANUP_TIMEOUT,
    ) -> None:
        """Cancel pending tasks and clean up worktree environments."""
        try:
            phase_timeout = float(cleanup_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("cleanup_timeout must be a finite number greater than zero") from exc
        if isinstance(cleanup_timeout, bool) or not math.isfinite(phase_timeout) or phase_timeout <= 0:
            raise ValueError("cleanup_timeout must be a finite number greater than zero")
        forced_timeout = min(
            MAX_FORCED_CLEANUP_TIMEOUT,
            max(0.1, phase_timeout),
        )

        # Validation precedes task creation so an invalid call is entirely
        # side-effect free. Concurrent cleanup callers share one teardown task;
        # cancellation of any waiter cannot cancel the resource owner.
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_impl(
                    phase_timeout=phase_timeout,
                    forced_timeout=forced_timeout,
                )
            )

        cancellation: asyncio.CancelledError | None = None
        cleanup_failure: BaseException | None = None
        while True:
            try:
                await asyncio.shield(self._cleanup_task)
                break
            except asyncio.CancelledError as exc:
                if self._cleanup_task.cancelled():
                    raise
                if cancellation is None:
                    cancellation = exc
                # Finish the already-bounded teardown before propagating caller
                # cancellation. A repeated caller cancellation is recorded by
                # the same loop and still cannot interrupt cleanup ownership.
                continue
            except BaseException as exc:
                cleanup_failure = exc
                break
        if cancellation is not None:
            if cleanup_failure is not None:
                add_note = getattr(cancellation, "add_note", None)
                if callable(add_note):
                    add_note(f"scheduler cleanup also failed: {type(cleanup_failure).__name__}: {cleanup_failure}")
            raise cancellation
        if cleanup_failure is not None:
            raise cleanup_failure

    async def _cleanup_impl(
        self,
        *,
        phase_timeout: float,
        forced_timeout: float,
    ) -> None:
        # This is the first teardown mutation. It runs in an owned task shielded
        # from every cleanup caller, including a caller cancelled mid-wait.
        self._shutting_down = True
        cleanup_failures: list[str] = []
        persistence_sessions = tuple(self._sessions.values())
        startup_aids = set(self._startup_tasks)
        cleanup_origin_snapshot = {
            **self._startup_origin,
            **self._spawn_origin,
        }
        execution_tasks = [
            *self._tasks.items(),
            *self._startup_tasks.items(),
        ]
        delivery_records = list(self._message_delivery_records.values())
        if self._lead_turn_record is not None:
            self._lead_turn_record["cleanup_terminal"] = True
            delivery_records.append(self._lead_turn_record)
        delivery_tasks = [
            *self._message_delivery_tasks.items(),
            *((0, task) for task in self._active_run_tasks),
        ]
        for record in delivery_records:
            add_task = record.get("add_task")
            if isinstance(add_task, asyncio.Task):
                delivery_tasks.append((int(record["aid"]), add_task))
        tracked_tasks = [*execution_tasks, *delivery_tasks]
        for task in {task for _, task in tracked_tasks}:
            if not task.done():
                task.cancel()

        await self._rollback_message_deliveries(
            delivery_records,
            timeout=forced_timeout,
        )

        pending = await self._wait_for_cleanup_tasks(
            {task for _, task in tracked_tasks},
            timeout=phase_timeout,
        )
        execution_required_forced_stop = False
        forced_execution_aids: set[int] = set()
        environment_abort_succeeded = True
        if pending:
            pending_aids = {aid for aid, task in execution_tasks if task in pending}
            if pending_aids:
                environment_abort_succeeded = await self._abort_session_environments(
                    pending_aids,
                    timeout=forced_timeout,
                )
            for task in pending:
                task.cancel()
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=forced_timeout,
            )
        if pending:
            execution_required_forced_stop = True
            forced_execution_aids = {aid for aid, task in execution_tasks if task in pending}
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(
                    task,
                    timeout=forced_timeout,
                )
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error("forced scheduler task termination failed: %s", error)
            pending = still_pending
        if execution_required_forced_stop:
            cleanup_failures.append("execution tasks did not quiesce")
        if not environment_abort_succeeded:
            cleanup_failures.append("session environment abort failed or timed out")

        await self._rollback_message_deliveries(
            delivery_records,
            timeout=forced_timeout,
        )

        # A Task cancelled before its coroutine gets its first timeslice never
        # enters ``_drive_agent``'s CancelledError handler. The same is true for
        # a stubborn task still alive after both bounded phases. Finalize both
        # states here so teardown cannot leave a scheduled ghost with a live
        # lease or an unresolved parent row.
        for aid, task in execution_tasks:
            if not (
                aid in forced_execution_aids
                or task in pending
                or task.cancelled()
                or self._task_finished_with_error(task)
            ):
                continue
            self._finalize_cleanup_failure(
                aid,
                origin_override=cleanup_origin_snapshot.get(aid),
            )
        for task in pending:
            task.add_done_callback(self._consume_background_task)
        for aid in startup_aids:
            self._finalize_cleanup_failure(
                aid,
                origin_override=cleanup_origin_snapshot.get(aid),
            )
            self.table.entries.pop(aid, None)
            self._sessions.pop(aid, None)
            self._locks.pop(aid, None)
            self._message_inbox.pop(aid, None)
        self._startup_tasks.clear()
        self._startup_envs.clear()
        self._startup_origin.clear()
        for record in delivery_records:
            aid = int(record["aid"])
            self._message_delivery_tasks.pop(aid, None)
            if self._message_delivery_records.get(aid) is record:
                self._message_delivery_records.pop(aid, None)
            if self._lead_turn_record is record:
                self._lead_turn_record = None
            add_task = record.get("add_task")
            if isinstance(add_task, asyncio.Task):
                self._attach_late_message_restore(aid, record, add_task)
            if record.get("cleanup_terminal", False):
                self._finalize_cleanup_failure(aid)
        self._active_run_tasks.clear()
        # Teardown is terminal for this scheduler instance. Release even a
        # seeded-but-never-run Lead lease and defensively clear any reservation /
        # dedup entry whose owner disappeared before it entered a tracked task.
        self._lead_reservation = None
        self._child_reservation.clear()
        self._reservation_baseline.clear()
        self._inflight.clear()
        self._inflight_key_of.clear()
        self._tasks.clear()

        persistence_quiesced = await self._wait_for_session_persistence(
            persistence_sessions,
            timeout=phase_timeout,
        )
        if not persistence_quiesced:
            for task in self._pending_session_persistence_tasks(persistence_sessions):
                task.cancel()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=forced_timeout,
            )

        if persistence_quiesced:
            self._autosave_all_sessions()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=phase_timeout,
            )
            if not persistence_quiesced:
                for task in self._pending_session_persistence_tasks(persistence_sessions):
                    task.cancel()
                persistence_quiesced = await self._wait_for_session_persistence(
                    persistence_sessions,
                    timeout=forced_timeout,
                )
        if persistence_quiesced:
            self._write_manifest()
            persistence_quiesced = await self._wait_for_session_persistence(
                persistence_sessions,
                timeout=phase_timeout,
            )
            if not persistence_quiesced:
                for task in self._pending_session_persistence_tasks(persistence_sessions):
                    task.cancel()
                persistence_quiesced = await self._wait_for_session_persistence(
                    persistence_sessions,
                    timeout=forced_timeout,
                )
        persistence_errors = self._session_persistence_errors(persistence_sessions)
        worktree_release_safe = not execution_required_forced_stop and not pending and environment_abort_succeeded
        if worktree_release_safe:
            worktree_release_succeeded = await self._release_worktree_pool_bounded(
                cleanup_timeout=phase_timeout,
                forced_timeout=forced_timeout,
            )
        else:
            worktree_release_succeeded = False
        if not persistence_quiesced:
            cleanup_failures.append("session-owned tasks did not quiesce")
        if persistence_errors:
            cleanup_failures.append("session persistence failed")
        if not worktree_release_succeeded:
            if worktree_release_safe:
                cleanup_failures.append("worktree pool release failed or timed out")
            else:
                cleanup_failures.append(
                    "worktree pool release skipped because execution ownership was not revoked and quiesced"
                )
        if cleanup_failures:
            failure = RuntimeError("technical scheduler cleanup failed: " + "; ".join(cleanup_failures))
            if persistence_errors:
                raise failure from persistence_errors[0]
            raise failure

    @staticmethod
    async def _wait_for_cleanup_tasks(tasks: set[asyncio.Task[Any]], *, timeout: float) -> set[asyncio.Task[Any]]:
        pending = {task for task in tasks if not task.done()}
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=max(0.0, timeout),
            )
        for task in tasks - pending:
            Scheduler._consume_background_task(task)
        return pending

    def _persistence_sessions(
        self,
        initial: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        sessions: list[Any] = []
        seen: set[int] = set()
        for session in (*initial, *self._sessions.values()):
            if id(session) in seen:
                continue
            seen.add(id(session))
            sessions.append(session)
        return tuple(sessions)

    def _pending_session_persistence_tasks(
        self,
        sessions: tuple[Any, ...],
    ) -> set[asyncio.Task[Any]]:
        pending: set[asyncio.Task[Any]] = set()
        current = asyncio.current_task()
        for session in self._persistence_sessions(sessions):
            for task in getattr(session, "pending_cleanup_tasks", ()):
                if isinstance(task, asyncio.Task) and not task.done() and task is not current:
                    pending.add(task)
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
        """Wait through one stable empty turn for subscriber-owned saves."""
        deadline = asyncio.get_running_loop().time() + timeout
        saw_empty = False
        while True:
            pending = self._pending_session_persistence_tasks(sessions)
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
            _done, still_pending = await asyncio.wait(
                pending,
                timeout=remaining,
            )
            if still_pending:
                return False

    def _session_persistence_errors(
        self,
        sessions: tuple[Any, ...],
    ) -> tuple[Exception, ...]:
        errors = list(self._scheduler_persistence_errors)
        for session in self._persistence_sessions(sessions):
            for error in getattr(session, "persistence_errors", ()):
                if isinstance(error, Exception):
                    errors.append(error)
        for subscriber in self._fallback_autosavers.values():
            if subscriber.last_error is not None:
                errors.append(subscriber.last_error)
        if self._manifest_subscriber is not None and self._manifest_subscriber.last_error is not None:
            errors.append(self._manifest_subscriber.last_error)
        return tuple(errors)

    @staticmethod
    def _consume_background_task(task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _task_finished_with_error(task: asyncio.Task[Any]) -> bool:
        if not task.done():
            return False
        try:
            return task.exception() is not None
        except asyncio.CancelledError:
            return True

    async def _rollback_message_deliveries(
        self,
        records: list[dict[str, Any]],
        *,
        timeout: float,
    ) -> None:
        async def rollback(record: dict[str, Any]) -> None:
            aid = int(record["aid"])
            lock = self._locks.setdefault(aid, asyncio.Lock())
            async with lock:
                self._rollback_message_delivery_locked(aid, record)

        rollback_tasks = {
            asyncio.create_task(rollback(record)) for record in records if not record.get("committed", False)
        }
        pending = await self._wait_for_cleanup_tasks(
            rollback_tasks,
            timeout=timeout,
        )
        for task in pending:
            task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=timeout,
            )
        if pending:
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(task, timeout=timeout)
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error(
                        "forced message rollback termination failed: %s",
                        error,
                    )
            pending = still_pending
        for task in pending:
            task.add_done_callback(self._consume_background_task)

    def _finalize_cleanup_failure(
        self,
        aid: int,
        *,
        origin_override: tuple[int, str] | None = None,
    ) -> None:
        reason = "Error: scheduler cleanup cancelled delegated work"
        self._release_reservations(aid)
        if aid in self._delivery_committed:
            return
        origin = self._spawn_origin.pop(aid, None)
        if origin is None:
            origin = self._startup_origin.pop(aid, None)
        if origin is None:
            origin = origin_override
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
                    logger.debug(
                        "cleanup could not fail pending row %s on aid %s",
                        tool_call_id,
                        parent_aid,
                    )
        scb = self.table.get(aid)
        if scb is not None:
            scb.state.cancel(reason)
            scb.result = reason

    async def _abort_session_environments(self, aids: set[int], *, timeout: float) -> bool:
        seen: set[int] = set()
        abort_tasks: set[asyncio.Task[Any]] = set()
        succeeded = True
        for aid in aids:
            session = self._sessions.get(aid)
            envs = [self._startup_envs.get(aid)]
            if session is not None:
                envs.append(getattr(session, "env", None))
                tool_execution = getattr(session, "tool_execution", None)
                envs.append(getattr(tool_execution, "environment", None))
            for env in envs:
                if env is None or id(env) in seen:
                    continue
                seen.add(id(env))
                try:
                    env._aborted = True
                except BaseException as exc:
                    succeeded = False
                    logger.error(
                        "session environment synchronous revoke failed for aid %s: %s",
                        aid,
                        exc,
                    )
                abort = getattr(env, "abort", None)
                if not callable(abort):
                    continue
                try:
                    result = abort()
                except BaseException as exc:
                    succeeded = False
                    logger.error("session environment abort failed for aid %s: %s", aid, exc)
                    continue
                if inspect.isawaitable(result):
                    try:
                        abort_tasks.add(asyncio.ensure_future(result))
                    except BaseException as exc:
                        succeeded = False
                        logger.error(
                            "session environment abort scheduling failed for aid %s: %s",
                            aid,
                            exc,
                        )
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()

        pending = await self._wait_for_cleanup_tasks(abort_tasks, timeout=timeout)
        if pending:
            succeeded = False
        for task in pending:
            task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=timeout,
            )
        if pending:
            still_pending: set[asyncio.Task[Any]] = set()
            for task in pending:
                termination = await force_task_terminal(task, timeout=timeout)
                if not (termination.terminal or termination.isolated):
                    still_pending.add(task)
                for error in termination.errors:
                    logger.error(
                        "forced session environment abort termination failed: %s",
                        error,
                    )
            pending = still_pending
        if pending:
            logger.error(
                "%s session environment abort task(s) remained active after cleanup timeout",
                len(pending),
            )
        for task in abort_tasks:
            if not task.done():
                task.add_done_callback(self._consume_background_task)
                continue
            if task.cancelled():
                succeeded = False
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                succeeded = False
            else:
                if error is not None:
                    succeeded = False
                    logger.error("session environment abort task failed: %s", error)
        return succeeded and not pending

    async def _release_worktree_pool_bounded(
        self,
        *,
        cleanup_timeout: float,
        forced_timeout: float,
    ) -> bool:
        try:
            result = self._worktree_pool.release()
        except BaseException as exc:
            logger.error("worktree pool release failed: %s", exc)
            return False
        if not inspect.isawaitable(result):
            return True
        try:
            task = asyncio.ensure_future(result)
        except BaseException as exc:
            logger.error("worktree pool release scheduling failed: %s", exc)
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return False
        pending = await self._wait_for_cleanup_tasks(
            {task},
            timeout=cleanup_timeout,
        )
        succeeded = not pending
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            pending = await self._wait_for_cleanup_tasks(
                pending,
                timeout=forced_timeout,
            )
        if pending:
            termination = await force_task_terminal(
                task,
                timeout=forced_timeout,
            )
            pending = set() if termination.terminal else {task}
            for error in termination.errors:
                logger.error("forced worktree release termination failed: %s", error)
        for pending_task in pending:
            pending_task.add_done_callback(self._consume_background_task)
        if pending:
            logger.error("worktree pool release remained active after cleanup timeout")
            return False
        if task.cancelled():
            return False
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return False
        if error is not None:
            logger.error("worktree pool release failed: %s", error)
            return False
        return succeeded
