from __future__ import annotations

import asyncio
import base64
import builtins
import json
import math
import posixpath
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from opencollab.adapters._env_base import (
    Environment,
    ExecResult,
    TextFileRange,
    _public_error_name,
    _track_shell_command,
)
from opencollab.adapters.llm import errors as llm_errors
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage, to_plain_data
from opencollab.adapters.tools.base import host_write_lock, host_write_locks
from opencollab.application._session_run_shared import _TeamBudgetStop, _TokenBudgetStop
from opencollab.application._session_run_usage import _normalize_completion_usage
from opencollab.application.async_timeout import await_owned_operation, consume_task_result
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.run_control import _RunCancelled
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP

_METADATA = {"context_window", "estimate_request_tokens", "estimate_context_tokens", "minimum_output_tokens"}
_COMPLETION = {"messages", "tools", "temperature", "thinking", "thinking_params", "reasoning_effort",
               "tool_choice", "top_p", "max_output_tokens", "response_session_id"}
_ENVIRONMENT = {"exec_cmd", "read_file", "read_text_range", "write_file", "write_temp_file", "remove_file",
                "resolve_search_path"}


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _restore_error(value: dict[str, Any]) -> Exception:
    error_type = value["type"]
    error: Exception
    if error_type == "_RunCancelled":
        error = _RunCancelled()
    elif error_type == "_TeamBudgetStop":
        error = _TeamBudgetStop()
    elif error_type == "_TokenBudgetStop":
        error = _TokenBudgetStop(
            reserved_input_tokens=value["reserved_input_tokens"], remaining_budget=value["remaining_budget"],
            minimum_output_tokens=value["minimum_output_tokens"],
        )
    else:
        cls = getattr(llm_errors, error_type, getattr(builtins, error_type, RuntimeError))
        if not isinstance(cls, type) or not issubclass(cls, Exception):
            cls = RuntimeError
        error = cls(value["message"])
    for name, item in value.items():
        if name not in {"type", "message"}:
            setattr(error, name, Usage(**item) if name == "usage" else item)
    return error


class _FileEdit:
    """Run native operations in the task holding their environment's edit lock."""

    def __init__(self, environment: Any, lock: AbstractAsyncContextManager[Any]) -> None:
        self.environment = environment
        self.ready = asyncio.get_running_loop().create_future()
        self.ready.add_done_callback(consume_task_result)
        self.calls: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future[Any]] | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._run(lock))

    async def _run(self, lock: AbstractAsyncContextManager[Any]) -> None:
        try:
            async with lock:
                self.ready.set_result(None)
                while (call := await self.calls.get()) is not None:
                    action, arguments, reply = call
                    if reply.cancelled():
                        continue
                    try:
                        value = await getattr(self.environment, action)(**arguments)
                    except asyncio.CancelledError:
                        reply.cancel()
                        raise
                    except Exception as exc:
                        if not reply.done():
                            reply.set_exception(exc)
                    else:
                        if not reply.done():
                            reply.set_result(value)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            raise
        finally:
            while not self.calls.empty():
                call = self.calls.get_nowait()
                if call is not None and not call[2].done():
                    call[2].set_exception(RuntimeError("file edit lock closed"))

    async def call(self, action: str, arguments: dict[str, Any]) -> Any:
        if self.task.done():
            self.task.result()
            raise RuntimeError("file edit lock closed")
        reply = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((action, arguments, reply))
        return await reply

    async def release(self) -> None:
        self.calls.put_nowait(None)
        await self.task


class AuthoredWorkflowBridge:
    """Own only this authored run's strategy pools and admitted operations."""

    def __init__(self, environment: Any, artifacts: str, pool: Any, llm: Any, runtime: Any,
                 *, allow_unisolated_shell: bool, budget: int | None = None,
                 remaining_budget: Callable[[], int | None] | None = None,
                 on_usage: Callable[[int], None] | None = None,
                 team_budget_exhausted: Callable[[], bool] | None = None,
                 cancel_requested: Callable[[], bool] | None = None) -> None:
        relative = posixpath.relpath(artifacts, environment.workspace)
        if relative == ".." or relative.startswith("../") or posixpath.isabs(relative):
            raise ValueError("child mailbox must be inside the member workspace")
        self._environment = environment
        self._directory = posixpath.join(artifacts, "bridge")
        self._pool, self._config, self._runtime = pool, llm, runtime
        self._llm = None
        self._allow_unisolated_shell = allow_unisolated_shell
        self._environments = {0: environment}
        self._next_environment = 0
        self.tokens = 0
        self._budget = budget
        self._remaining_budget = remaining_budget
        self._on_usage = on_usage
        self._team_budget_exhausted = team_budget_exhausted
        self._cancel_requested = cancel_requested
        self._tasks: dict[str, asyncio.Task] = {}
        self._handles: dict[str, int] = {}
        self._edits: dict[str, _FileEdit] = {}
        self._cancelled_tasks: set[asyncio.Task[Any]] = set()

    def _cancel(self, task: asyncio.Task[Any]) -> None:
        if not task.done() and task not in self._cancelled_tasks:
            self._cancelled_tasks.add(task)
            task.cancel()

    def _model(self) -> Any:
        if self._llm is None:
            aliases = {"llm_timeout": "request_timeout", "llm_max_retries": "max_retries",
                       "llm_connect_timeout": "connect_timeout", "llm_first_event_timeout": "first_event_timeout",
                       "llm_stream_idle_timeout": "stream_idle_timeout", "llm_stream_chat": "stream_chat"}
            allowed = {"model", "provider", "api_key", "auth_token", "base_url", "wire_protocol", "context_window",
                       "provider_error_time_budget", "request_pool", "request_capacity", *aliases}
            self._llm = LLMClient(**{aliases.get(key, key): value for key, value in self._config.items()
                                    if key in allowed and (value is not None or key == "context_window")})
        return self._llm

    def options(self) -> dict[str, Any]:
        return {"strategies": self._pool.strategy_names, "isolation": self._pool.default_isolation,
                "metadata": tuple(_METADATA),
                "requires_thinking_prefix": None,
                "supports_request_admission": self._config.get("request_pool") is not None,
                "supports_response_session_identity": self._config.get("wire_protocol") == "responses"}

    def _request_output_limit(self, arguments: dict[str, Any]) -> int:
        if self._cancel_requested is not None and self._cancel_requested():
            raise _RunCancelled
        if self._team_budget_exhausted is not None and self._team_budget_exhausted():
            raise _TeamBudgetStop
        configured = arguments.get("max_output_tokens")
        if configured is None:
            configured = self._config.get("max_output_tokens", DEFAULT_MAX_TOKENS_PER_STEP)
        remaining = None if self._budget is None else self._budget - self.tokens
        if self._remaining_budget is not None:
            parent_remaining = self._remaining_budget()
            if parent_remaining is not None:
                remaining = parent_remaining if remaining is None else min(remaining, parent_remaining)
        if remaining is None:
            return configured
        model = self._model()
        thinking = arguments.get("thinking", False)
        thinking_params = arguments.get("thinking_params")
        reserved = model.estimate_request_tokens(
            arguments["messages"], arguments.get("tools"), thinking=thinking, thinking_params=thinking_params,
        )
        minimum = model.minimum_output_tokens(
            max_output_tokens=configured, thinking=thinking, thinking_params=thinking_params,
        )
        if remaining - reserved < minimum:
            raise _TokenBudgetStop(
                reserved_input_tokens=reserved, remaining_budget=remaining, minimum_output_tokens=minimum,
            )
        return min(configured, remaining - reserved)

    def _account_native_usage(self, usage: Any, *, accounted_tokens: int = 0) -> None:
        self.tokens += _normalize_completion_usage(usage)[1] - accounted_tokens
        if self._on_usage is not None:
            self._on_usage(self.tokens)

    async def poll(self) -> None:
        try:
            requests = json.loads(await self._environment.read_file(posixpath.join(self._directory, "requests.json")))
        except FileNotFoundError:
            return
        if not isinstance(requests, dict):
            raise ValueError("invalid child bridge requests")
        for identity, request in requests.items():
            if not identity.isdecimal() or len(identity) > 12 or not isinstance(request, dict):
                raise ValueError("invalid child bridge request identity")
            handle = request.get("environment", 0)
            if isinstance(handle, bool) or not isinstance(handle, int):
                raise ValueError("invalid child bridge environment handle")
            if identity not in self._tasks:
                self._tasks[identity] = asyncio.create_task(self._respond(identity, request))
                self._handles[identity] = handle
            elif request.get("cancel"):
                self._cancel(self._tasks[identity])
                if identity in self._edits:
                    self._cancel(self._edits[identity].task)
        for identity, task in tuple(self._tasks.items()):
            if identity not in requests and task.done():
                task.result()
                self._cancelled_tasks.discard(task)
                del self._tasks[identity]
                del self._handles[identity]

    async def _respond(self, identity: str, request: dict[str, Any]) -> None:
        try:
            if request.get("cancel"):
                raise asyncio.CancelledError
            payload = json.loads(await await_owned_operation(self._environment.read_file(
                posixpath.join(self._directory, f"{identity}.request.json"),
            ), propagate_cancellation=True))
            if not isinstance(payload, dict):
                raise ValueError("bridge request payload must be an object")
            reply = {"value": await self._dispatch(identity, {**payload, **request})}
        except BaseException as exc:
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
            error = {"type": _public_error_name(exc), "message": str(exc)}
            for name in ("usage", "status_code", "code", "param", "body", "reserved_input_tokens",
                         "remaining_budget", "minimum_output_tokens"):
                value = getattr(exc, name, None)
                if value is not None:
                    error[name] = (
                        asdict(value) if name == "usage" and isinstance(value, Usage) else to_plain_data(value)
                    )
            reply = {"error": error}
        await await_owned_operation(self._environment.write_file(
            posixpath.join(self._directory, f"{identity}.json"), json.dumps(reply, ensure_ascii=False),
        ))

    async def _dispatch(self, identity: str, request: dict[str, Any]) -> Any:
        action, arguments = request["action"], request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("bridge arguments must be an object")
        handle = request.get("environment", 0)
        if isinstance(handle, bool) or not isinstance(handle, int) or handle not in self._environments:
            raise ValueError("unknown environment for this authored run")
        environment = self._environments[handle]
        if action in {"file_write_lock", "file_write_locks"}:
            required = {"path"} if action == "file_write_lock" else {"paths"}
            optional = {"shared_ancestors"} if action == "file_write_locks" else set()
            if not required <= arguments.keys() <= required | optional:
                raise ValueError("file edit lock needs paths")
            paths = [arguments["path"]] if action == "file_write_lock" else arguments["paths"]
            if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
                raise ValueError("file edit lock needs non-empty text paths")
            lock = (
                host_write_lock(paths[0], environment) if action == "file_write_lock"
                else host_write_locks(paths, environment, shared_ancestors=arguments.get("shared_ancestors") is True)
            )
            edit = _FileEdit(environment, lock)
            self._edits[identity] = edit
            try:
                await asyncio.shield(edit.ready)
            except BaseException:
                self._cancel(edit.task)
                await await_owned_operation(asyncio.gather(edit.task, return_exceptions=True))
                del self._edits[identity]
                raise
            return identity
        if action == "file_write_unlock":
            if set(arguments) != {"id"}:
                raise ValueError("file edit unlock needs its owned lock")
            edit = self._edits.get(arguments["id"])
            if edit is None or edit.environment is not environment:
                raise ValueError("unknown file edit lock for this environment")
            await edit.release()
            del self._edits[arguments["id"]]
            return None
        if action == "metadata":
            name = arguments["name"]
            if name == "requires_thinking_prefix":
                return self._model().requires_thinking_prefix
            if name not in _METADATA:
                raise ValueError("unknown model metadata operation")
            return getattr(self._model(), name)(*arguments.get("args", []), **arguments.get("kwargs", {}))
        if action == "complete":
            if set(arguments) - _COMPLETION:
                raise ValueError("unknown completion controls")
            notifications = []
            native_accounting = getattr(self._model(), "supports_request_revalidation", False)
            if native_accounting:
                accounted_tokens = 0

                def account_attempt(usage: Any) -> None:
                    nonlocal accounted_tokens
                    total = _normalize_completion_usage(usage)[1]
                    accounted_tokens += total
                    self._account_native_usage(usage)

                arguments["before_request"] = lambda: self._request_output_limit(arguments)
                arguments["on_usage"] = lambda usage: self._account_native_usage(
                    usage, accounted_tokens=accounted_tokens,
                )
                if getattr(self._model(), "supports_attempt_usage", False):
                    arguments["on_attempt_usage"] = account_attempt
            if getattr(self._model(), "supports_request_admission", False):
                def admitted() -> None:
                    notifications.append(asyncio.create_task(self._environment.write_file(
                        posixpath.join(self._directory, f"{identity}.admitted"), "ready",
                    )))
                arguments["on_admitted"] = admitted
            try:
                try:
                    response = await self._model().complete(**arguments)
                except BaseException as exc:
                    if not native_accounting and getattr(exc, "usage", None) is not None:
                        self._account_native_usage(exc.usage)
                    raise
                if not native_accounting:
                    self._account_native_usage(response.usage)
                return to_plain_data(response)
            finally:
                if notifications:
                    await await_owned_operation(asyncio.gather(*notifications), propagate_cancellation=True)
        if action == "acquire":
            if set(arguments) != {"strategy", "label"}:
                raise ValueError("acquire accepts a strategy and label")
            acquired = await self._pool.acquire(arguments["label"], source=environment,
                                                isolation=arguments["strategy"])
            self._next_environment += 1
            self._environments[self._next_environment] = acquired
            return {"handle": self._next_environment, "workspace": acquired.workspace,
                    "remote_filesystem": getattr(acquired, "remote_filesystem", False) is True,
                    "process_isolated": bool(getattr(acquired, "process_isolated", False))}
        if action == "release":
            if arguments or handle == 0:
                raise ValueError("release needs an owned child environment")
            pending = [task for key, task in self._tasks.items() if key != identity and not task.done()
                       and self._environments.get(self._handles[key]) is environment]
            pending.extend(edit.task for edit in self._edits.values()
                           if edit.environment is environment and not edit.task.done())
            for task in pending:
                self._cancel(task)
            await asyncio.gather(*pending, return_exceptions=True)
            await self._pool.release_env(environment)
            del self._environments[handle]
            return None
        if action not in _ENVIRONMENT:
            raise ValueError("unknown child environment operation")
        if action == "exec_cmd":
            if not self._allow_unisolated_shell and not getattr(environment, "process_isolated", False):
                raise PermissionError("child command requires process isolation")
            from opencollab.bootstrap.container import build_workspace_safety_policy
            policy = build_workspace_safety_policy(environment)
            if policy is not None:
                await policy.check_cmd_interactive(arguments["cmd"], self._runtime.confirm_fn())
            if arguments.get("timeout", 120) is None:
                arguments["timeout"] = math.inf
            if "input_bytes" in arguments:
                arguments["input_bytes"] = base64.b64decode(arguments["input_bytes"], validate=True)
        edit_id = request.get("edit")
        with _track_shell_command(environment) if action == "exec_cmd" else nullcontext():
            if edit_id is None:
                result = await getattr(environment, action)(**arguments)
            else:
                edit = self._edits.get(edit_id)
                if edit is None or edit.environment is not environment:
                    raise ValueError("unknown file edit lock for this environment")
                result = await edit.call(action, arguments)
        if isinstance(result, (ExecResult, TextFileRange)):
            result = asdict(result)
            if result.get("stdout_bytes") is not None:
                result["stdout_bytes"] = base64.b64encode(result["stdout_bytes"]).decode()
        return result

    async def aclose(self) -> None:
        tasks = [*self._tasks.values(), *(edit.task for edit in self._edits.values())]
        for task in tasks:
            self._cancel(task)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if self._llm is not None:
            await self._llm.close()
        await self._pool.release()
        failures = [item for item in results if isinstance(item, Exception)]
        if failures:
            raise OSError(f"child bridge response persistence failed: {failures[0]}") from failures[0]


class BridgeClient:
    def __init__(self, artifacts: Path, options: dict[str, Any]) -> None:
        self.directory = artifacts / "bridge"
        self.directory.resolve().relative_to(Path.cwd().resolve())
        self.directory.mkdir(parents=True, exist_ok=True)
        self.options = options
        self._next = 0
        self._pending: dict[str, Any] = {}

    def _publish(self) -> None:
        _write_json(self.directory / "requests.json", self._pending)

    def _begin(self, action: str, arguments: dict[str, Any], environment: int, edit: str | None = None) -> str:
        self._next += 1
        identity = str(self._next)
        payload: dict[str, Any] = {"action": action, "arguments": arguments}
        if edit is not None:
            payload["edit"] = edit
        _write_json(self.directory / f"{identity}.request.json", payload)
        self._pending[identity] = {"environment": environment}
        self._publish()
        return identity

    def _result(self, identity: str) -> Any:
        path = self.directory / f"{identity}.json"
        reply = json.loads(path.read_text(encoding="utf-8"))
        del self._pending[identity]
        self._publish()
        (self.directory / f"{identity}.request.json").unlink(missing_ok=True)
        if "error" in reply:
            raise _restore_error(reply["error"])
        return reply["value"]

    def metadata(self, name: str, *args: Any, **kwargs: Any) -> Any:
        identity = self._begin("metadata", {"name": name, "args": args, "kwargs": kwargs}, 0)
        while not (self.directory / f"{identity}.json").exists():
            time.sleep(0.01)
        return self._result(identity)

    async def call(self, action: str, arguments: dict[str, Any], environment: int = 0,
                   *, on_admitted: Any = None, edit: str | None = None) -> Any:
        identity = self._begin(action, arguments, environment, edit)
        try:
            while not (self.directory / f"{identity}.json").exists():
                if on_admitted is not None and (self.directory / f"{identity}.admitted").exists():
                    on_admitted()
                    on_admitted = None
                await asyncio.sleep(0.01)
            return self._result(identity)
        except asyncio.CancelledError as cancellation:
            self._pending[identity]["cancel"] = True
            self._publish()

            async def acknowledged() -> Any:
                while not (self.directory / f"{identity}.json").exists():
                    await asyncio.sleep(0.01)
                return self._result(identity)

            try:
                value = await await_owned_operation(acknowledged(), propagate_cancellation=False)
            except Exception as exc:
                usage = getattr(exc, "usage", None)
            else:
                usage = Usage(**value["usage"]) if action == "complete" and isinstance(value, dict) else None
                if action in {"file_write_lock", "file_write_locks"}:
                    try:
                        await await_owned_operation(self.call("file_write_unlock", {"id": value}, environment))
                    except Exception as exc:
                        add_exception_note(cancellation, f"file edit lock cleanup also failed: {exc}")
            if usage is not None:
                cancellation.usage = usage
            raise

    def provider(self, strategy: str, **_kwargs: Any) -> Any:
        client = self

        class Pool:
            async def acquire(self, role: str, *, source: Any = None) -> Any:
                handle = source.handle if isinstance(source, BridgeEnvironment) else 0
                result = await client.call("acquire", {"strategy": strategy, "label": role}, handle)
                return BridgeEnvironment(client, result)

            async def release_env(self, environment: Any) -> None:
                await environment.cleanup()

            async def release(self) -> None:
                pass

        return Pool()


class BridgeEnvironment(Environment):
    _handles_file_lock_hierarchy = True

    def __init__(self, client: BridgeClient, options: dict[str, Any]) -> None:
        super().__init__()
        self.client, self.handle = client, options["handle"]
        self.workspace, self.process_isolated = options["workspace"], options["process_isolated"]
        self.remote_filesystem = options.get("remote_filesystem") is True
        self._released = False
        self._edit: ContextVar[str | None] = ContextVar("authored_file_edit", default=None)

    async def _call(self, action: str, arguments: dict[str, Any]) -> Any:
        return await self.client.call(action, arguments, self.handle, edit=self._edit.get())

    def file_write_lock(self, path: str) -> AbstractAsyncContextManager[None]:
        return self._file_edit("file_write_lock", {"path": path})

    def file_write_locks(
        self, paths: Sequence[str], *, _shared_ancestors: bool = False,
    ) -> AbstractAsyncContextManager[None]:
        arguments: dict[str, Any] = {"paths": list(paths)}
        if _shared_ancestors:
            arguments["shared_ancestors"] = True
        return self._file_edit("file_write_locks", arguments)

    @asynccontextmanager
    async def _file_edit(self, action: str, arguments: dict[str, Any]) -> AsyncIterator[None]:
        identity = await self.client.call(action, arguments, self.handle)
        token = self._edit.set(identity)
        try:
            yield None
        finally:
            self._edit.reset(token)
            await await_owned_operation(
                self.client.call("file_write_unlock", {"id": identity}, self.handle),
                propagate_cancellation=True,
            )

    async def exec_cmd(self, cmd: str, timeout: float = 120.0, *, input_bytes: bytes | None = None) -> ExecResult:
        arguments: dict[str, Any] = {"cmd": cmd, "timeout": timeout if math.isfinite(timeout) else None}
        if input_bytes is not None:
            arguments["input_bytes"] = base64.b64encode(input_bytes).decode()
        result = await self._call("exec_cmd", arguments)
        if result.get("stdout_bytes") is not None:
            result["stdout_bytes"] = base64.b64decode(result["stdout_bytes"])
        return ExecResult(**result)

    async def read_file(self, path: str) -> str:
        return await self._call("read_file", {"path": path})

    async def resolve_search_path(self, path: str) -> str:
        return await self._call("resolve_search_path", {"path": path})

    async def read_text_range(self, path: str, **kwargs: Any) -> TextFileRange:
        return TextFileRange(**await self._call("read_text_range", {"path": path, **kwargs}))

    async def write_file(self, path: str, content: str) -> None:
        await self._call("write_file", {"path": path, "content": content})

    async def write_temp_file(self, content: str, *, prefix: str, suffix: str = ".tmp") -> str:
        return await self._call("write_temp_file", {"content": content, "prefix": prefix, "suffix": suffix})

    async def remove_file(self, path: str) -> None:
        await self._call("remove_file", {"path": path})

    async def cleanup(self) -> None:
        if not self._released:
            async def release() -> None:
                await self.client.call("release", {}, self.handle)
                self._released = True
            await await_owned_operation(release(), propagate_cancellation=True)


class BridgeLLM:
    def __init__(self, client: BridgeClient, environment: Any) -> None:
        self.client = client
        self.handle = environment.handle if isinstance(environment, BridgeEnvironment) else 0
        self.supports_response_session_identity = client.options["supports_response_session_identity"]
        self.supports_request_admission = client.options["supports_request_admission"]

    @property
    def requires_thinking_prefix(self) -> bool:
        return self.client.metadata("requires_thinking_prefix")

    def context_window(self) -> int | None:
        return self.client.metadata("context_window")

    def estimate_request_tokens(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, thinking: bool = False, thinking_params: dict[str, Any] | None = None,
    ) -> int:
        return self.client.metadata("estimate_request_tokens", messages, tools,
                                    thinking=thinking, thinking_params=thinking_params)

    def estimate_context_tokens(
        self, messages: list[dict[str, Any]], *, thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
    ) -> int:
        return self.client.metadata("estimate_context_tokens", messages,
                                    thinking=thinking, thinking_params=thinking_params)

    def minimum_output_tokens(
        self, *, max_output_tokens: int, thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
    ) -> int:
        return self.client.metadata("minimum_output_tokens", max_output_tokens=max_output_tokens,
                                    thinking=thinking, thinking_params=thinking_params)

    async def complete(self, messages: list[dict[str, Any]], *, on_admitted: Any = None, **kwargs: Any) -> LLMResponse:
        result = await self.client.call(
            "complete", {"messages": messages, **kwargs}, self.handle, on_admitted=on_admitted,
        )
        result["usage"] = Usage(**result["usage"])
        return LLMResponse(**result)

    async def close(self) -> None:
        pass
