"""Async tools with JSON Schema inputs and string results."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import posixpath
import resource
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from typing import Any

from filelock import FileLock, Timeout

from opencollab.adapters.tools._file_lock_worker import _try_file_lock
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.tool_execution import DeferredCall, ToolRuntime

HOST_WRITE_LOCK_TIMEOUT_SECONDS = 10.0
HOST_WRITE_LOCK_POLL_SECONDS = 0.02

def _host_lock_root() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = os.path.join(tempfile.gettempdir(), f"opencollab-write-locks-{uid}")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root

def _host_lock_path(path: str, env: Any) -> str:
    workspace = getattr(env, "workspace", "") if env is not None else ""
    identity = os.path.abspath(os.path.join(workspace, path)) if workspace else os.path.abspath(path)
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    return os.path.join(_host_lock_root(), f"{digest}.lock")

def _write_lock_paths(
    paths: Sequence[str], env: Any, *, workspace: str | None = None,
) -> list[str]:
    local = bool(getattr(env, "local_filesystem", False))
    path_module = os.path if local else posixpath
    workspace = path_module.abspath(workspace or getattr(env, "workspace", "") or ".")
    if local:
        workspace = os.path.realpath(workspace)
    targets = set()
    for path in paths:
        target = path_module.normpath(path_module.join(workspace, path))
        if local:
            target = os.path.join(os.path.realpath(os.path.dirname(target)), os.path.basename(target))
        targets.add(target)
        parent = path_module.dirname(target)
        while parent != workspace and parent.startswith(workspace.rstrip("/") + "/"):
            targets.add(parent)
            parent = path_module.dirname(parent)
    return sorted(targets)


def _local_file_lock_capacity() -> int:
    limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if limit == resource.RLIM_INFINITY:
        return sys.maxsize
    # Leave descriptors for the environment's subprocesses and pending I/O.
    return max(1, limit - max(16, limit // 4))


def _available_local_file_locks(capacity: int) -> int:
    try:
        return capacity - len(os.listdir("/dev/fd"))
    except OSError as exc:
        return 0 if exc.errno == errno.EMFILE else capacity


async def _reap_lock_holders(processes: list[subprocess.Popen]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while pending := [process for process in processes if process.poll() is None]:
        if loop.time() >= deadline:
            for process in pending:
                process.kill()
            deadline = float("inf")
        await asyncio.sleep(HOST_WRITE_LOCK_POLL_SECONDS)


async def _prepare_lock_holder(
    channel: socket.socket, paths: Sequence[str], env: Any, shared_paths: frozenset[str],
) -> None:
    loop = asyncio.get_running_loop()
    channel.setblocking(False)
    options = (
        [(path, _host_lock_path(path, env), path in shared_paths) for path in paths],
        HOST_WRITE_LOCK_TIMEOUT_SECONDS, HOST_WRITE_LOCK_POLL_SECONDS,
    )
    await loop.sock_sendall(channel, json.dumps(options).encode() + b"\n")
    response = bytearray()
    while chunk := await loop.sock_recv(channel, 4096):
        response.extend(chunk)
        if chunk.endswith(b"\n"):
            break
    else:
        raise RuntimeError("local write lock holder exited during acquisition")
    status, detail = json.loads(response)
    if status == "timeout":
        raise TimeoutError(detail)
    if status != "ready":
        raise RuntimeError(f"local write lock holder failed: {detail}")


@contextlib.asynccontextmanager
async def _hold_lock_groups(
    paths: Sequence[str], env: Any, group_size: int, shared_paths: frozenset[str],
) -> AsyncIterator[None]:
    owner = asyncio.current_task()
    assert owner is not None
    processes: list[subprocess.Popen] = []
    ready = 0
    worker = os.path.join(os.path.dirname(__file__), "_file_lock_worker.py")

    async def watch_exit() -> None:
        while True:
            for index in range(ready):
                returncode = processes[index].poll()
                if returncode is not None:
                    owner.cancel(f"local write lock holder exited unexpectedly ({returncode})")
                    return
            await asyncio.sleep(HOST_WRITE_LOCK_POLL_SECONDS)

    # One group can use its startup socket as the ownership signal.
    shared = len(paths) > group_size
    read_end, write_end = os.pipe() if shared else (-1, -1)
    ownership_socket = None
    monitor = asyncio.create_task(watch_exit())
    try:
        for start in range(0, len(paths), group_size):
            setup, child = socket.socketpair()
            if not shared:
                ownership_socket = setup
            try:
                with child:
                    # Isolate terminal interrupts and avoid per-child pidfds.
                    processes.append(subprocess.Popen(
                        [sys.executable, worker, str(read_end if shared else 0)], start_new_session=True,
                        stdin=child, stdout=child, pass_fds=(read_end,) if shared else (),
                    ))
                await _prepare_lock_holder(setup, paths[start:start + group_size], env, shared_paths)
            finally:
                if shared:
                    setup.close()
            ready += 1
        if shared:
            os.close(read_end)
            read_end = -1
        yield
    finally:
        monitor.cancel()
        if read_end >= 0:
            os.close(read_end)
        if shared:
            os.close(write_end)
        if ownership_socket is not None:
            ownership_socket.close()
        await await_owned_operation(_reap_lock_holders(processes), propagate_cancellation=True)


@contextlib.asynccontextmanager
async def _local_write_locks(
    paths: Sequence[str], env: Any, *, shared_paths: frozenset[str] = frozenset(),
) -> AsyncIterator[FileLock | None]:
    capacity = _local_file_lock_capacity()
    if len(paths) == 1 or len(paths) <= _available_local_file_locks(capacity):
        async with contextlib.AsyncExitStack() as locks:
            target = None
            try:
                for path in paths:
                    target = await locks.enter_async_context(_target_write_lock(path, env, shared=path in shared_paths))
            except OSError as exc:
                if exc.errno != errno.EMFILE:
                    raise
            else:
                yield target
                return
    # Every holder uses the same per-path locks. Contention order stays the
    # same across groups, direct callers and older installations.
    async with _hold_lock_groups(paths, env, min(512, capacity), shared_paths):
        yield None


@contextlib.asynccontextmanager
async def host_write_locks(paths: Sequence[str], env: Any) -> AsyncIterator[Any]:
    """Own edited paths and their ancestors through publication or recovery."""
    if env is None or getattr(env, "local_filesystem", False):
        async with _local_write_locks(_write_lock_paths(paths, env), env) as target:
            yield target
        return
    grouped = getattr(env, "file_write_locks", None)
    if callable(grouped):
        async with grouped(paths) as target:
            yield target
        return
    if getattr(env, "file_write_lock_scope", None) == "workspace":
        async with _target_write_lock(paths[0], env) as target:
            yield target
        return
    async with contextlib.AsyncExitStack() as locks:
        target = None
        for path in _write_lock_paths(paths, env):
            target = await locks.enter_async_context(_target_write_lock(path, env))
        yield target


@contextlib.asynccontextmanager
async def host_write_lock(path: str, env: Any) -> AsyncIterator[Any]:
    if env is None or getattr(env, "local_filesystem", False):
        paths = _write_lock_paths((path,), env)
        # The single target sorts after its ancestors.
        async with _local_write_locks(paths, env, shared_paths=frozenset(paths[:-1])) as target:
            yield target
        return
    lock = (
        _target_write_lock(path, env) if getattr(env, "_delegates_file_write_lock", False)
        else host_write_locks((path,), env)
    )
    async with lock as target:
        yield target


@contextlib.asynccontextmanager
async def _target_write_lock(path: str, env: Any, *, shared: bool = False) -> AsyncIterator[Any]:
    if env is not None and not getattr(env, "local_filesystem", False):
        environment_lock = getattr(env, "file_write_lock", None)
        if callable(environment_lock):
            async with environment_lock(path) as target:
                yield target
        else:
            yield None
        return

    lock_path = _host_lock_path(path, env)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HOST_WRITE_LOCK_TIMEOUT_SECONDS
    with contextlib.ExitStack() as held:
        while True:
            try:
                lock = held.enter_context(_try_file_lock(lock_path, shared=shared))
                break
            except Timeout:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"timed out acquiring write lock for {path}")
                await asyncio.sleep(min(HOST_WRITE_LOCK_POLL_SECONDS, remaining))
        yield lock

class Tool:
    """Base class for all tools. Subclass and implement ``execute_with_runtime``.

    Attributes:
        name: Tool name as seen by the LLM (snake_case recommended).
        description: One-line description for the LLM to understand when to use it.
        parameters: JSON Schema dict describing the input parameters.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    default_timeout: float | None = None
    disable_outer_timeout: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str | DeferredCall:
        """Execute the tool with the runtime bundle.

        Returns the result as a string; a deferrable tool returns a
        ``DeferredCall`` instead when it hands work off.
        """
        raise NotImplementedError(
            f"Tool '{self.name}' must implement execute_with_runtime()"
        )
