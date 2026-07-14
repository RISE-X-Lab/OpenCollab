from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from opencollab.application.tool_execution import ToolRuntime


def _tool_execution_module():
    return sys.modules["opencollab.application.tool_execution"]


class ToolExecutionRuntimeMixin:
    async def execute_tool(
        self,
        tool,
        args: dict,
        *,
        tool_id: str | None = None,
    ) -> tuple[str, float]:
        """Run one tool, mapping any exception to an error string.

        Returns ``(output, latency_seconds)`` — never raises, so one failing
        tool cannot abort the rest of the batch.
        """
        start = _tool_execution_module().time.monotonic()
        runtime = self.tool_runtime(tool_call_id=tool_id)
        timeout = self.tool_execution_timeout(tool, args)
        execution_task: asyncio.Task[Any] | None = None
        try:
            execution = tool.execute_with_runtime(args, runtime)
            execution_task = asyncio.ensure_future(execution)
            result = await self._await_execution_task(execution_task, timeout)
        except _tool_execution_module()._ToolExecutionTimeoutError:
            tool_name = self._tool_display_name(tool)
            timeout_result = f"Tool execution timed out after {timeout:.1f}s while running '{tool_name}'."
            if execution_task is None:
                result = timeout_result
            else:
                cleanup_task = asyncio.create_task(self._cleanup_timed_out_execution(execution_task))
                try:
                    quiesced, revoke_error, abort_error = await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await self._await_owned_cleanup_despite_cancellation(cleanup_task)
                    raise
                if quiesced:
                    result = timeout_result
                else:
                    details = [
                        "Tool cancellation cleanup failed after two bounded cancellation attempts",
                        "the execution environment was revoked before returning",
                    ]
                    if revoke_error:
                        details.append(revoke_error)
                    if abort_error:
                        details.append(abort_error)
                    result = timeout_result + " " + "; ".join(details) + "."
        except asyncio.CancelledError:
            if execution_task is not None and not execution_task.done():
                cleanup_task = asyncio.create_task(self._cleanup_caller_cancelled_execution(execution_task))
                await self._await_owned_cleanup_despite_cancellation(cleanup_task)
            raise
        except PermissionError as e:
            result = f"Permission denied: {e}"
        except Exception as e:
            result = f"Tool execution error: {type(e).__name__}: {e}"
        finally:
            if execution_task is not None and not execution_task.done():
                self._track_pending_cleanup(execution_task)

        return result, _tool_execution_module().time.monotonic() - start

    @staticmethod
    async def _await_execution_task(task: asyncio.Task[Any], timeout: float | None) -> Any:
        if timeout is None:
            return await asyncio.shield(task)
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return task.result()
        task.cancel()
        raise _tool_execution_module()._ToolExecutionTimeoutError

    async def _quiesce_cancelled_task(self, task: asyncio.Task[Any]) -> bool:
        """Give one cancellation time to unwind, then cancel once more."""
        if await self._wait_task(task, self._cancellation_cleanup_timeout):
            return True
        task.cancel()
        return await self._wait_task(task, self._cancellation_force_timeout)

    async def _cleanup_caller_cancelled_execution(self, task: asyncio.Task[Any]) -> None:
        task.cancel()
        await self._cleanup_timed_out_execution(task)

    async def _cleanup_timed_out_execution(self, task: asyncio.Task[Any]) -> tuple[bool, str | None, str | None]:
        if await self._quiesce_cancelled_task(task):
            return True, None, None
        self._track_pending_cleanup(task)
        revoke_error = self._revoke_environment_sync()
        abort_error = await self._abort_environment_bounded()
        termination = await _tool_execution_module().force_task_terminal(
            task,
            timeout=self._cancellation_force_timeout,
        )
        if termination.errors:
            detail = "; ".join(str(error) for error in termination.errors)
            abort_error = "; ".join(part for part in (abort_error, detail) if part)
        return False, revoke_error, abort_error

    async def _await_owned_cleanup_despite_cancellation(self, cleanup_task: asyncio.Task[Any]) -> None:
        """Finish owned cleanup even when the caller repeats ``Task.cancel``."""
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                if cleanup_task.done():
                    self._consume_task_result(cleanup_task)
                    return
                continue
            except BaseException:
                self._consume_task_result(cleanup_task)
                return

    @staticmethod
    async def _wait_task(task: asyncio.Task[Any], timeout: float) -> bool:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if done:
            _tool_execution_module().ToolExecutionUseCase._consume_task_result(task)
        return bool(done)

    def _track_pending_cleanup(self, task: asyncio.Task[Any]) -> None:
        if task.done() or task in self._pending_cleanup_tasks:
            return
        self._pending_cleanup_tasks.add(task)
        task.add_done_callback(self._pending_cleanup_tasks.discard)
        task.add_done_callback(self._consume_task_result)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def _revoke_environment_sync(self) -> str | None:
        """Synchronously block concrete Environment methods before abort awaits."""
        if self.environment is None:
            return "no execution environment was available to revoke"
        try:
            setattr(self.environment, "_aborted", True)
        except Exception as exc:  # pragma: no cover - exotic immutable ports
            return f"environment revocation failed: {type(exc).__name__}: {exc}"
        return None

    async def _abort_environment_bounded(self) -> str | None:
        if self.environment is None:
            return "environment abort was unavailable"
        abort = getattr(self.environment, "abort", None)
        if not callable(abort):
            return "environment abort was unavailable"
        try:
            outcome = abort()
        except Exception as exc:
            return f"environment abort failed: {type(exc).__name__}: {exc}"
        if not _tool_execution_module().inspect.isawaitable(outcome):
            return None

        try:
            abort_task = asyncio.ensure_future(outcome)
        except Exception as exc:
            close = getattr(outcome, "close", None)
            if callable(close):
                close()
            return f"environment abort scheduling failed: {type(exc).__name__}: {exc}"
        try:
            if await self._wait_task(abort_task, self._environment_abort_timeout):
                return self._task_failure(abort_task, label="environment abort")
            abort_task.cancel()
            if await self._wait_task(abort_task, self._cancellation_force_timeout):
                failure = self._task_failure(abort_task, label="environment abort")
                return failure or "environment abort timed out and was cancelled"
            termination = await _tool_execution_module().force_task_terminal(
                abort_task,
                timeout=self._cancellation_force_timeout,
            )
            if not termination.terminal:
                self._track_pending_cleanup(abort_task)
            detail = "; ".join(str(error) for error in termination.errors)
            return "environment abort did not quiesce within its bounded timeout" + (f": {detail}" if detail else "")
        except asyncio.CancelledError:
            abort_task.cancel()
            self._track_pending_cleanup(abort_task)
            raise

    @staticmethod
    def _task_failure(task: asyncio.Task[Any], *, label: str) -> str | None:
        try:
            task.result()
        except asyncio.CancelledError:
            return f"{label} was cancelled"
        except Exception as exc:
            return f"{label} failed: {type(exc).__name__}: {exc}"
        return None

    def _tool_display_name(self, tool: Any) -> str:
        return str(getattr(tool, "name", type(tool).__name__))

    def tool_execution_timeout(self, tool: Any, args: dict) -> float | None:
        if getattr(tool, "disable_outer_timeout", False):
            return None
        requested = self._numeric_timeout((args or {}).get("timeout"))
        base = requested if requested is not None else self._tool_default_timeout(tool)
        return min(
            base + _tool_execution_module().TOOL_EXECUTION_TIMEOUT_GRACE,
            _tool_execution_module().MAX_TOOL_EXECUTION_TIMEOUT,
        )

    def _tool_default_timeout(self, tool: Any) -> float:
        configured = self._numeric_timeout(getattr(tool, "default_timeout", None))
        if configured is not None:
            return configured
        return _tool_execution_module().DEFAULT_TOOL_EXECUTION_TIMEOUT

    def _numeric_timeout(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return None
        if not _tool_execution_module().math.isfinite(timeout) or timeout <= 0:
            return None
        return timeout

    def tool_runtime(self, tool_call_id: str | None = None) -> ToolRuntime:
        return _tool_execution_module().ToolRuntime(
            environment=self.environment,
            safety_policy=self.safety_policy,
            permission_policy=self.permission_policy,
            ask_policy=self.ask_policy,
            aid=self.state.aid,
            tool_call_id=tool_call_id,
        )

    async def execute_deferred(self, tc: dict) -> tuple[int | None, str | None]:
        """Drive a single deferrable tool (e.g. ``spawn_agent``).

        Returns ``(ref, None)`` when the tool deferred work and handed back a
        :class:`DeferredCall` (its ``ref`` — a child aid — is awaited), or
        ``(None, error_text)`` when it resolved synchronously (bad args,
        unknown tool, permission/topology rejection, or a plain string return)
        and its row should fill at once. The per-call ``tool_call_id`` is
        threaded into the runtime so the scheduler can route the eventual
        completion back to the right pending row.

        Deferred tools bypass ``process`` (and thus loop-detection hashing) by
        design — a spawn is never a doom-loop the way a repeated read is.
        """
        func = tc["function"]
        tool_name = func["name"]
        try:
            args = self.parse_tool_args(func)
        except _tool_execution_module().json.JSONDecodeError:
            raw_args = str(func.get("arguments", ""))
            return None, f"Error: invalid JSON arguments: {raw_args[:200]}"
        except ValueError:
            raw_args = str(func.get("arguments", ""))
            return None, (f"Error: tool arguments must be a JSON object: {raw_args[:200]}")

        tool = self.find_tool(tool_name)
        if not tool:
            return None, f"Error: unknown tool '{tool_name}'."

        await self._emit_observation(
            lambda: self.event_factory.tool_start(tool_name, args),
            label="tool_start",
        )

        latency = 0.0
        try:
            outcome, latency = await self.execute_tool(tool, args, tool_id=tc["id"])
        finally:
            await self._emit_observation(
                lambda: self.event_factory.tool_end(tool_name, latency),
                label="tool_end",
            )

        if isinstance(outcome, _tool_execution_module().DeferredCall):
            return outcome.ref, None
        return None, str(outcome)

    def _trace_short_circuit(self, step_type: str, tool_name: str, detail: dict[str, Any]) -> None:
        """Record a tracer step for a pre-execution short-circuit.

        The three branches that answer a tool call without ever executing it
        (malformed JSON args, unknown tool, loop-detection block) previously left
        no trajectory trace, so a run could short-circuit silently. This logs a
        distinct ``step_type`` ("tool_error"/"loop_blocked") with the attempted
        tool and a small args snapshot. Observability only — no behavior change;
        a no-op when no tracer is wired.
        """
        if not self.tracer:
            return
        payload: dict[str, Any] = {"tool": tool_name}
        payload.update(detail)
        # Cap any args snapshot so a trace record can't balloon.
        if isinstance(payload.get("args"), dict):
            snapshot = _tool_execution_module().json.dumps(payload["args"], default=str)[:500]
            payload["args"] = snapshot
        self._trace_observation(step_type=step_type, payload=payload)

    async def _emit_observation(self, build_event: Callable[[], Any], *, label: str) -> None:
        """Keep event sinks observational even when a direct publisher fails."""
        try:
            await self.event_publisher.emit(build_event())
        except Exception as exc:
            _tool_execution_module().logger.error("%s event failed: %s", label, exc)

    def _trace_observation(self, **payload: Any) -> None:
        if not self.tracer:
            return
        try:
            self.tracer.log_step(**payload)
        except Exception as exc:
            _tool_execution_module().logger.error("tool trace failed: %s", exc)

    def trace_payload(self, tool_name: str, args: dict, tool_output: str) -> dict[str, Any]:
        # Cap result in trace to 4k to keep trajectory files manageable.
        trace_result = (
            tool_output
            if len(tool_output) <= 4096
            else tool_output[:2048] + "\n...[truncated]...\n" + tool_output[-2048:]
        )
        return {
            "tool": tool_name,
            "args": args,
            "result_len": len(tool_output),
            "result": trace_result,
        }

    def tool_result_message(self, tool_id: str, result: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_id, "content": result}
