"""Own member-authored Workflow processes through an existing Environment."""

from __future__ import annotations

import asyncio
import json
import math
import posixpath
import re
import shlex
import tempfile
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from opencollab.adapters._docker_snapshot import _copy_artifact_to_host
from opencollab.adapters._env_base import ExecResult, _track_shell_command
from opencollab.adapters._env_process import ProcessCleanupError
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.adapters.tools._paths import checked_path
from opencollab.adapters.tools.base import Tool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.isolation import isolation_name
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.authored_workflow_bridge import AuthoredWorkflowBridge
from opencollab.bootstrap.tool_registry import TOOL_LIMIT_FIELDS

if TYPE_CHECKING:
    from opencollab.application.ports import EnvironmentPort
    from opencollab.application.session import Session
    from opencollab.bootstrap.context_builder import SpawnConfig

_RUNNER_STOP_GRACE_SECONDS = 5.0
_ISOLATION_DESCRIPTIONS = {
    "shared": "Share the member's execution environment and workspace.",
    "worktree": "Use separate worktrees inside the inherited execution environment.",
    "docker-group": "Share a dedicated container with separate worktrees for child agents.",
    "docker-session": "Use a dedicated container and workspace for each child agent.",
    "vm-group": "Lease one configured Linux VM with separate worktrees for child agents.",
    "vm-session": "Lease a separate configured Linux VM for each child agent.",
}


@dataclass
class _Child:
    id: str
    environment: EnvironmentPort
    artifacts: str
    budget: int | None
    cleanup_timeout: float = 2.0
    task: asyncio.Task[None] | None = None
    execution: asyncio.Task[Any] | None = None
    stop_requested: bool = False
    status: str = "running"
    tokens: int = 0
    reported_tokens: int = 0
    accounting_settled: bool = False
    result: dict[str, Any] | None = None
    cleanup_error: Exception | None = None
    bridge: AuthoredWorkflowBridge | None = None
    saved_artifacts: str | None = None
    artifacts_removed: bool = False


class AuthoredWorkflowTool(Tool):
    name = "manage_workflow"
    description = (
        "Execute a Workflow you wrote in a Python source file in your execution environment. "
        "Before authoring source, call describe for the configured isolation strategies, inherited tool permissions "
        "and a minimal authoring example. "
        "launch returns a child id immediately. Use status, wait, stop or result with that id. "
        "entry defaults to main and accepts (ctx, args). Tokens share your Team budget. "
        "The environment's Python must have OpenCollab installed."
    )
    disable_outer_timeout = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["describe", "launch", "status", "wait", "stop", "result"]},
            "id": {"type": "string", "description": "Required for status, wait, stop and result. "
                                                     "Use the id from launch."},
            "source": {"type": "string", "description": "Required for launch. Python file inside your workspace."},
            "entry": {"type": "string", "default": "main"},
            "args": {"type": "object"},
            "python": {"type": "string", "default": "python3"},
            "budget": {"type": "integer", "minimum": 1},
            "concurrency": {"type": "integer", "minimum": 1, "default": 4},
            "cleanup_timeout": {"type": "number", "minimum": 0,
                                "description": "launch only. Positive cleanup grace in seconds. "
                                               "Omitted inherits the parent."},
            "isolation": {"type": "string", "description": "launch only. Default strategy for child agents. "
                          "Omitted inherits the member strategy."},
            "timeout": {"type": "number", "minimum": 0,
                        "description": "Positive wait duration in seconds. Expiry leaves the child running. "
                                       "Use status for an immediate observation."},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self, get_session: Callable[[], Session], config: SpawnConfig,
        save_dir: str | None, *, allow_unisolated_shell: bool = False,
        environment_pool_factory: Callable[..., Any] | None = None,
        environment_catalog: Callable[[], dict[str, Any]] | None = None,
        describe_environment: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._get_session = get_session
        self._config = config
        self._save_dir = save_dir
        self._allow_unisolated_shell = allow_unisolated_shell
        self._environment_pool_factory = environment_pool_factory
        self._environment_catalog = environment_catalog
        self._describe_environment = describe_environment
        self.parameters = deepcopy(type(self).parameters)
        self._children: dict[str, _Child] = {}
        self._registered = False
        self._launch_lock = asyncio.Lock()

    def _catalog(self) -> dict[str, Any]:
        if self._describe_environment is not None:
            parent = self._get_session()
            return self._describe_environment(parent.env, role=parent.agent.name)
        if self._environment_catalog is not None:
            return self._environment_catalog()
        return {"default_isolation": "shared",
                "strategies": ("shared", "worktree", "docker-group", "docker-session")}

    def to_openai_schema(self) -> dict[str, Any]:
        catalog = self._catalog()
        self.parameters["properties"]["isolation"].update(
            enum=list(catalog["strategies"]), default=catalog["default_isolation"],
            description="launch only. Default strategy for child agents. Configured strategies are listed; "
                        "resources are checked on allocation. Omitted inherits " + catalog["default_isolation"] + ".",
        )
        return super().to_openai_schema()

    def _describe(self, runtime: ToolRuntime) -> str:
        from opencollab.tools import BuiltinToolName

        parent = self._get_session()
        catalog = self._catalog()
        shell = parent.agent.find_tool("bash")
        isolated = bool(getattr(runtime.environment, "process_isolated", False))
        launch_allowed = runtime.environment is not None and not runtime.environment.revoked and shell is not None and (
            isolated or (self._allow_unisolated_shell and not getattr(shell, "require_process_isolation", False))
        )
        allowed = [tool.name for tool in parent.agent.tools]
        return json.dumps({
            "default_isolation": catalog["default_isolation"],
            "strategies": [{"name": name,
                            "description": _ISOLATION_DESCRIPTIONS.get(name, "Configured custom environment provider."),
                            "availability": "checked_on_allocation"}
                           for name in catalog["strategies"]],
            "launch_allowed_in_current_environment": launch_allowed,
            "launch_requirements": "The member needs bash permission and an OS process sandbox or explicit "
                                   "host-shell permission. Its Python must have OpenCollab installed.",
            "allowed_child_tools": allowed,
            "builtin_child_tools": [name for name in allowed if name in get_args(BuiltinToolName)],
            "entry": "An async function main(ctx, args), or a named entry, returning a JSON-serializable value.",
            "example": "async def main(ctx, args):\n"
                       "    return await ctx.parallel([\n"
                       "        lambda task=task: ctx.agent(task, tools=[])\n"
                       "        for task in args['tasks']\n"
                       "    ])\n",
            "tools": "Import builtin_tools from opencollab.tools to construct names from builtin_child_tools. "
                     "Child tool names "
                     "must come from allowed_child_tools and retain the member's limits. Individual "
                     "ctx.agent calls can select isolation from the configured strategies.",
            "lifecycle": "launch returns an id. Use wait or result with that id to collect the output. "
                         "status observes progress; stop cancels the owned run. Tokens share the Team budget.",
        }, ensure_ascii=False)

    async def execute_with_runtime(self, params: dict[str, Any], runtime: ToolRuntime) -> str:
        if params["action"] == "describe":
            return self._describe(runtime)
        parent = self._get_session()
        if not self._registered:
            parent.own_runtime(self.aclose, reserve_tokens=self._reserved_tokens)
            self._registered = True
        action = params["action"]
        if action != "launch" and (extra := {"isolation", "cleanup_timeout"} & params.keys()):
            raise ValueError(f"{' and '.join(sorted(extra))} is only supported by the launch action")
        if action == "launch":
            async with self._launch_lock:
                child = await self._launch(params, runtime)
        else:
            child = self._children.get(params.get("id", ""))
            if child is None:
                raise ValueError("unknown child run for this Team member")
            if action == "stop":
                await self._stop(child)
            elif action == "wait":
                assert child.task is not None
                timeout = params.get("timeout")
                if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
                    raise ValueError("timeout must be finite and positive")
                try:
                    await asyncio.wait_for(asyncio.shield(child.task), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            elif action not in {"status", "result"}:
                raise ValueError("unknown Workflow action")
        return json.dumps(self._snapshot(child, include_result=action in {"wait", "stop", "result"}),
                          ensure_ascii=False)

    def _snapshot(self, child: _Child, *, include_result: bool) -> dict[str, Any]:
        record = {"id": child.id, "status": child.status, "tokens": child.tokens,
                  "artifacts": child.saved_artifacts or child.artifacts}
        if include_result:
            record["result"] = child.result
        return record

    def _reserved_tokens(self) -> int:
        # Retain native spend while the child's usage file catches up.
        return sum(max(0, (item.budget or 0) - item.tokens,
                       (item.bridge.tokens if item.bridge is not None else item.tokens) - item.tokens)
                   for item in self._children.values() if item.status == "running" and not item.accounting_settled)

    def _remaining_child_budget(self, child: _Child) -> int | None:
        remaining = self._get_session().runner.remaining_budget_tokens
        if remaining is None:
            return None
        spent = max(child.tokens, child.bridge.tokens if child.bridge is not None else 0)
        return remaining + max(0, (child.budget or 0) - spent)

    async def _launch(self, params: dict[str, Any], runtime: ToolRuntime) -> _Child:
        parent = self._get_session()
        environment = runtime.environment
        if environment is None or environment.revoked:
            raise RuntimeError("no active execution environment available")
        shell = parent.agent.find_tool("bash")
        if shell is None:
            raise PermissionError("manage_workflow source execution requires the member's bash permission")
        isolated = bool(getattr(environment, "process_isolated", False))
        if (not self._allow_unisolated_shell or getattr(shell, "require_process_isolation", False)) and not isolated:
            raise PermissionError("manage_workflow requires an OS process sandbox or allow_unisolated_shell")
        if parent.runner._run_cancellation_requested() or parent.state.phase.is_terminal():
            raise RuntimeError("parent session is stopping")
        source = params.get("source")
        if not isinstance(source, str) or not source or "\x00" in source:
            raise ValueError("source must name a Python file in the workspace")
        source = checked_path(runtime, source, native_file=True)
        await environment.read_file(source)
        budget = params.get("budget")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
            raise ValueError("budget must be a positive integer")
        remaining = parent.runner.remaining_budget_tokens
        if remaining is not None:
            if remaining <= 0:
                raise ValueError("Team session token budget is exhausted or reserved by child runs")
            budget = max(1, remaining // 2) if budget is None else min(budget, remaining)
        cleanup_timeout = params.get("cleanup_timeout", self._config.workflow_cleanup_timeout)
        if (
            isinstance(cleanup_timeout, bool) or not isinstance(cleanup_timeout, (int, float))
            or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0
        ):
            raise ValueError("cleanup_timeout must be finite and positive")
        pool = (self._environment_pool_factory(environment, role=parent.agent.name)
                if self._environment_pool_factory is not None else SessionEnvironmentPool(
                    environment.workspace, isolation="shared", base_environment=environment,
                ))
        strategy = None
        try:
            if "isolation" in params:
                strategy = isolation_name(params["isolation"])
                if strategy not in pool.strategy_names:
                    raise ValueError(f"isolation strategy {strategy!r} needs an environment provider")
        except BaseException:
            await pool.release()
            raise
        child_id = f"child-{uuid.uuid4().hex}"
        artifacts = posixpath.join(environment.workspace, ".opencollab", "child-runs", child_id)
        child = _Child(child_id, environment, artifacts, budget, cleanup_timeout=cleanup_timeout)
        config = {key: value for key, value in {**vars(self._config), **vars(parent.agent)}.items()
                  if key in {"model", "provider", "wire_protocol", "api_key", "auth_token", "base_url",
                             "temperature", "top_p", "thinking", "thinking_params", "reasoning_effort",
                             "context_window", "llm_timeout", "llm_max_retries", "llm_connect_timeout",
                             "llm_first_event_timeout", "llm_stream_idle_timeout", "llm_stream_chat",
                             "provider_error_time_budget", "thinking_params_explicit", "model_routes",
                             "request_pool", "request_capacity", "logical_model"}}
        config["max_output_tokens"] = parent.agent.max_tokens_per_step
        tool_limits = {
            tool.name: {key: getattr(tool, key)
                        for key in (*TOOL_LIMIT_FIELDS.get(tool.name, ()), "allow_create", "require_process_isolation")
                        if hasattr(tool, key)}
            for tool in parent.agent.tools
        }
        request = {"id": child_id, "source": source, "entry": params.get("entry", "main"),
                   "args": params.get("args", {}), "config": config, "budget": budget,
                   "max_steps": parent.max_steps, "concurrency": params.get("concurrency", 4),
                   "cleanup_timeout": cleanup_timeout,
                   "allowed_tools": [tool.name for tool in parent.agent.tools],
                   "tool_limits": tool_limits,
                   "process_isolated": isolated,
                   "artifacts": artifacts}
        command = " ".join([shlex.quote(params.get("python", "python3")), "-m",
                            "opencollab.bootstrap.authored_workflow_runner"])
        if runtime.safety_policy is not None:
            await runtime.safety_policy.check_cmd_interactive(command, runtime.confirm_fn())
        child.bridge = AuthoredWorkflowBridge(
            environment, artifacts, pool, config, runtime,
            budget=budget,
            remaining_budget=lambda: self._remaining_child_budget(child),
            on_usage=lambda tokens: self._account_usage(child, tokens, reported=False),
            team_budget_exhausted=parent.runner._team_budget_exhausted,
            cancel_requested=lambda: child.stop_requested or parent.runner._run_cancellation_requested(),
            allow_unisolated_shell=self._allow_unisolated_shell
            and not getattr(shell, "require_process_isolation", False),
        )
        request["bridge"] = child.bridge.options()
        if strategy is not None:
            request["bridge"]["isolation"] = strategy
        request["config"] = {key: value for key, value in config.items() if key in {
            "model", "logical_model", "provider", "wire_protocol", "temperature", "top_p",
            "thinking", "thinking_params", "thinking_params_explicit", "reasoning_effort",
            "context_window", "max_output_tokens", "llm_timeout", "llm_max_retries",
            "llm_connect_timeout", "llm_first_event_timeout", "llm_stream_idle_timeout",
            "llm_stream_chat", "provider_error_time_budget",
        }}
        request_bytes = json.dumps(request, allow_nan=False).encode("utf-8")
        self._children[child_id] = child
        child.task = asyncio.create_task(self._execute(child, command, request_bytes))
        return child

    async def _refresh_usage(self, child: _Child) -> None:
        try:
            usage = json.loads(await child.environment.read_file(posixpath.join(child.artifacts, "usage.json")))
        except FileNotFoundError:
            return
        self._account_usage(child, usage["tokens"])

    def _account_usage(self, child: _Child, tokens: Any, *, reported: bool = True) -> None:
        minimum = child.reported_tokens if reported else child.tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < minimum:
            raise ValueError("invalid child usage total")
        if reported:
            child.reported_tokens = tokens
        delta = max(child.tokens, tokens) - child.tokens
        if delta:
            parent = self._get_session()
            parent.state.add_used_tokens(delta)
            parent.enqueue_auto_save()
            child.tokens += delta

    async def _execute(self, child: _Child, command: str, request_bytes: bytes) -> None:
        async def run() -> ExecResult:
            with _track_shell_command(child.environment):
                return await child.environment.exec_cmd(command, timeout=math.inf, input_bytes=request_bytes)

        execution = None
        logs: dict[str, Any] = {}
        status = "running"
        result: dict[str, Any] | None = None
        saved_artifacts: str | None = None
        try:
            execution = asyncio.create_task(run())
            child.execution = execution
            if child.stop_requested:
                execution.cancel()
            while not execution.done():
                await asyncio.wait({execution}, timeout=0.1)
                assert child.bridge is not None
                await child.bridge.poll()
                await self._refresh_usage(child)
                if self._get_session().runner._run_cancellation_requested():
                    await child.environment.write_file(posixpath.join(child.artifacts, "stop"), "stop")
            outcome = execution.result()
            logs = {"returncode": outcome.returncode, "stdout": outcome.stdout, "stderr": outcome.stderr}
            if outcome.returncode:
                raise RuntimeError(f"Workflow runner exited {outcome.returncode}: {outcome.stderr[-2000:]}")
            with tempfile.TemporaryDirectory(prefix="opencollab-child-result-") as temporary:
                result_path = Path(temporary) / "result.json"
                await _copy_artifact_to_host(
                    child.environment, posixpath.join(child.artifacts, "result.json"), result_path,
                )
                result = json.loads(result_path.read_text(encoding="utf-8"))
            self._account_usage(child, result["tokens"])
            status = result["status"]
        except asyncio.CancelledError:
            status = "stopped"
            result = {"status": "stopped", "output": None, "reason": "cancelled"}
        except Exception as exc:
            status = "failed"
            result = {"status": "failed", "output": None, "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            if execution is not None:
                cancelling = not execution.done()
                if cancelling:
                    execution.cancel()
                settled = await asyncio.gather(execution, return_exceptions=True)
                if isinstance(settled[0], ProcessCleanupError) or (cancelling and isinstance(settled[0], Exception)):
                    status = "failed"
                    child.cleanup_error = settled[0]
                    result = {"status": "failed", "output": None,
                              "reason": f"child process cleanup failed: {settled[0]}"}
            try:
                assert child.bridge is not None
                await await_owned_operation(child.bridge.aclose(), propagate_cancellation=False)
                await self._refresh_usage(child)
                self._account_usage(child, max(child.tokens, child.bridge.tokens), reported=False)
            except Exception as exc:
                status = "failed"
                child.cleanup_error = exc
                result = {"status": "failed", "output": None,
                          "reason": f"{(result or {}).get('reason', '')}; child accounting failed: {exc}"}
            if result is not None:
                result["tokens"] = child.tokens
            # Artifact export can outlast the token reservation.
            child.accounting_settled = True
            if self._save_dir is not None:
                try:
                    directory = Path(tempfile.mkdtemp(prefix=f"{child.id}-", dir=self._save_dir))
                    native_files = (result or {}).get("artifact_files")
                    final_inventory = native_files is not None
                    if native_files is None:
                        try:
                            native_files = json.loads(await child.environment.read_file(
                                posixpath.join(child.artifacts, "artifact_files.json"),
                            ))
                        except FileNotFoundError:
                            native_files = []
                    for name in native_files:
                        if not isinstance(name, str) or not re.fullmatch(
                            r"(?:workflow\.json|\d+(?:_[^/\n]+)?\.json)(?:\.journal(?:\.lock)?)?", name,
                        ):
                            raise ValueError("invalid child artifact name")
                        try:
                            await _copy_artifact_to_host(
                                child.environment, posixpath.join(child.artifacts, name), directory / name,
                            )
                        except FileNotFoundError:
                            # The provisional list can name files the child never created.
                            if final_inventory:
                                raise
                    saved_artifacts = str(directory)
                    terminal = replace(child, status=status, result=result, saved_artifacts=saved_artifacts)
                    (directory / "process.json").write_text(json.dumps(logs), encoding="utf-8")
                    (directory / "result.json").write_text(json.dumps(self._snapshot(terminal, include_result=True)),
                                                           encoding="utf-8")
                except Exception as exc:
                    status = "failed"
                    child.cleanup_error = exc
                    reason = f"child result persistence failed: {exc}"
                    if result and result.get("reason"):
                        reason = f"{result['reason']}. {reason}"
                    result = {"status": "failed", "output": None,
                              "reason": reason, "tokens": child.tokens}
            child.saved_artifacts = saved_artifacts
            child.result = result
            child.status = status

    async def _stop(self, child: _Child) -> None:
        if child.task is None or child.task.done():
            return
        child.stop_requested = True
        try:
            await child.environment.write_file(posixpath.join(child.artifacts, "stop"), "stop")
            await asyncio.wait_for(asyncio.shield(child.task),
                                   timeout=max(_RUNNER_STOP_GRACE_SECONDS, child.cleanup_timeout))
        except (OSError, RuntimeError, asyncio.TimeoutError):
            if child.execution is not None and not child.execution.done():
                child.execution.cancel()
            await await_owned_operation(
                asyncio.gather(child.task, return_exceptions=True), propagate_cancellation=False,
            )
        if child.task.cancelled():
            child.status = "stopped"
            child.result = {"status": "stopped", "output": None, "reason": "cancelled", "tokens": child.tokens}

    async def aclose(self) -> None:
        failures = []
        async with self._launch_lock:
            for child in self._children.values():
                try:
                    await self._stop(child)
                except Exception as exc:
                    failures.append(exc)
                if child.cleanup_error is not None:
                    failures.append(child.cleanup_error)
                elif not child.artifacts_removed:
                    try:
                        result = await child.environment.exec_cmd(f"rm -rf -- {shlex.quote(child.artifacts)}")
                        if result.returncode:
                            raise OSError("child runtime artifact cleanup failed")
                        child.artifacts_removed = True
                    except Exception as exc:
                        failures.append(exc)
        if failures:
            raise RuntimeError(f"member-authored Workflow cleanup failed: {failures[0]}") from failures[0]
