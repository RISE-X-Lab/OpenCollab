"""ShellHookRunner — runs configured hook actions on a lifecycle event.

Phase 1 ships one executor: ``command`` runs a shell command with the event
payload as JSON on stdin and a few convenience env vars. It is observe-only — a
nonzero exit or a timeout is logged, never raised — so a misbehaving hook can
neither stall nor crash an agent.

The ``prompt`` and ``agent`` executor keys are reserved: ``agent`` is the
team-coordination bridge, which is why the runner accepts a ``scheduler`` handle
even though phase 1 never uses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable

from opencollab.application.ports import HookPort
from opencollab.domain.hooks import HookOutcome, HookSpec, match_hooks

logger = logging.getLogger(__name__)

CommandExecutor = Callable[[HookSpec, dict[str, Any]], Awaitable[None]]
_WORKER_POLL_SECONDS = 0.05
_TERM_GRACE_SECONDS = 0.25
_KILL_REAP_SECONDS = 1.0
_CALLER_CLEANUP_OBSERVE_SECONDS = 0.1
_OWNED_PROCESS_CLEANUP_OBSERVE_SECONDS = (
    _TERM_GRACE_SECONDS + _KILL_REAP_SECONDS + 0.25
)


def _subprocess_group_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        }
    return {}


def _signal_posix_group(proc: subprocess.Popen[Any], sig: signal.Signals) -> bool:
    try:
        os.killpg(proc.pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.warning("hook process-group signal failed (pid=%s): %s", proc.pid, exc)
        return False


def _posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_single_process(proc: subprocess.Popen[Any], *, hard: bool) -> bool:
    try:
        if hard:
            proc.kill()
        else:
            proc.terminate()
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.warning("hook process signal failed (pid=%s): %s", proc.pid, exc)
        return False


def _taskkill_windows_tree(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("taskkill failed for hook pid=%s: %s", pid, exc)
        return False
    return completed.returncode == 0


def _wait_for_leader(proc: subprocess.Popen[Any], timeout: float) -> bool:
    try:
        proc.wait(timeout=max(0.0, timeout))
        return True
    except (subprocess.TimeoutExpired, ChildProcessError):
        return False
    except OSError as exc:
        logger.warning("hook process wait failed (pid=%s): %s", proc.pid, exc)
        return False


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> bool:
    """Synchronously terminate descendants and reap the owned shell."""
    if os.name == "posix":
        pgid = proc.pid
        _signal_posix_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while _posix_group_exists(pgid) and time.monotonic() < deadline:
            _wait_for_leader(proc, min(_WORKER_POLL_SECONDS, deadline - time.monotonic()))
            if _posix_group_exists(pgid):
                time.sleep(min(_WORKER_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        if _posix_group_exists(pgid):
            _signal_posix_group(proc, signal.SIGKILL)
        kill_deadline = time.monotonic() + _KILL_REAP_SECONDS
        leader_reaped = False
        group_gone = False
        while time.monotonic() < kill_deadline:
            remaining = kill_deadline - time.monotonic()
            leader_reaped = (
                _wait_for_leader(proc, min(_WORKER_POLL_SECONDS, remaining))
                or leader_reaped
            )
            group_gone = not _posix_group_exists(pgid)
            if leader_reaped and group_gone:
                break
            time.sleep(
                min(
                    _WORKER_POLL_SECONDS,
                    max(0.0, kill_deadline - time.monotonic()),
                )
            )
        if not leader_reaped or not group_gone:
            logger.warning(
                "hook process tree did not quiesce after forced termination (pid=%s)",
                proc.pid,
            )
        return leader_reaped and group_gone

    if os.name == "nt":
        tree_killed = _taskkill_windows_tree(proc.pid)
        if not tree_killed:
            _signal_single_process(proc, hard=True)
        return _wait_for_leader(proc, _KILL_REAP_SECONDS)

    _signal_single_process(proc, hard=False)
    if _wait_for_leader(proc, _TERM_GRACE_SECONDS):
        return True
    _signal_single_process(proc, hard=True)
    return _wait_for_leader(proc, _KILL_REAP_SECONDS)


def _hook_worker(
    *,
    command: str,
    env: dict[str, str],
    stdin_bytes: bytes,
    deadline: float,
    stop: threading.Event,
    done: threading.Event,
    state: dict[str, Any],
) -> None:
    """Own spawn through teardown independently of the asyncio event loop."""
    proc: subprocess.Popen[Any] | None = None
    try:
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **_subprocess_group_kwargs(),
            )
        except BaseException as exc:
            state.update(status="spawn_error", error=exc)
            return

        state["pid"] = proc.pid
        if stop.is_set():
            state.update(status="cancelled", cleanup_ok=_terminate_process_tree(proc))
            return
        if time.monotonic() >= deadline:
            state.update(status="timeout", cleanup_ok=_terminate_process_tree(proc))
            return

        first_communicate = True
        while True:
            if stop.is_set():
                state.update(status="cancelled", cleanup_ok=_terminate_process_tree(proc))
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.update(status="timeout", cleanup_ok=_terminate_process_tree(proc))
                return
            try:
                proc.communicate(
                    stdin_bytes if first_communicate else None,
                    timeout=min(_WORKER_POLL_SECONDS, remaining),
                )
                cleanup_ok = True
                if os.name == "posix" and _posix_group_exists(proc.pid):
                    cleanup_ok = _terminate_process_tree(proc)
                state.update(
                    status="completed" if cleanup_ok else "cleanup_failed",
                    returncode=proc.returncode,
                    cleanup_ok=cleanup_ok,
                )
                return
            except subprocess.TimeoutExpired:
                first_communicate = False
            except BaseException as exc:
                state.update(
                    status="transport_error",
                    error=exc,
                    cleanup_ok=_terminate_process_tree(proc),
                )
                return
    finally:
        if proc is not None:
            for pipe_name in ("stdin", "stdout", "stderr"):
                pipe = getattr(proc, pipe_name, None)
                if pipe is None:
                    continue
                try:
                    pipe.close()
                except BaseException as exc:
                    logger.warning(
                        "hook process pipe close failed (pid=%s, pipe=%s): %s",
                        proc.pid,
                        pipe_name,
                        exc,
                    )
        done.set()


async def _wait_for_worker(
    done: threading.Event,
    *,
    deadline: float,
    stop: threading.Event,
) -> bool:
    while not done.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            stop.set()
            return done.is_set()
        await asyncio.sleep(_WORKER_POLL_SECONDS)
    return True


class ShellHookRunner(HookPort):
    def __init__(self, specs: tuple[HookSpec, ...], *, scheduler: Any = None):
        self._specs = specs
        self._scheduler = scheduler
        self.cleanup_quiesced = True
        self._executors: dict[str, CommandExecutor] = {
            "command": self._run_command,
        }

    async def fire(self, event_name: str, payload: dict[str, Any]) -> HookOutcome:
        for spec in match_hooks(self._specs, event_name, payload.get("tool")):
            executor = self._executors.get(spec.action_type)
            if executor is None:
                raise NotImplementedError(f"Hook action type '{spec.action_type}' is not implemented.")
            await executor(spec, payload)
        return HookOutcome()

    async def _run_command(self, spec: HookSpec, payload: dict[str, Any]) -> None:
        env = {
            **os.environ,
            "OPENCOLLAB_HOOK_EVENT": str(payload.get("hook_event_name", "")),
            "OPENCOLLAB_TOOL": str(payload.get("tool", "")),
            "OPENCOLLAB_AID": str(payload.get("aid", "")),
        }
        try:
            timeout = float(spec.timeout)
        except (TypeError, ValueError):
            logger.warning("hook command has invalid timeout %r: %s", spec.timeout, spec.command)
            return
        if not math.isfinite(timeout) or timeout <= 0:
            logger.warning("hook command has invalid timeout %r: %s", spec.timeout, spec.command)
            return
        try:
            stdin_bytes = json.dumps(payload).encode()
        except (TypeError, ValueError) as exc:
            logger.warning("hook payload could not be serialized (%s): %s", spec.command, exc)
            return

        loop = asyncio.get_running_loop()
        command_deadline = time.monotonic() + timeout
        stop = threading.Event()
        done = threading.Event()
        state: dict[str, Any] = {}
        worker = threading.Thread(
            target=_hook_worker,
            kwargs={
                "command": spec.command,
                "env": env,
                "stdin_bytes": stdin_bytes,
                "deadline": command_deadline,
                "stop": stop,
                "done": done,
                "state": state,
            },
            name="opencollab-hook-command",
            # Keep the PID owner alive through interpreter shutdown. A daemon
            # worker can be cut off between TERM and KILL when the caller exits
            # immediately after receiving cancellation.
            daemon=False,
        )
        worker.start()

        outer_deadline = loop.time() + timeout
        try:
            completed = await _wait_for_worker(done, deadline=outer_deadline, stop=stop)
        except BaseException:
            stop.set()
            cleanup_observe = (
                _OWNED_PROCESS_CLEANUP_OBSERVE_SECONDS
                if "pid" in state
                else _CALLER_CLEANUP_OBSERVE_SECONDS
            )
            cleanup_deadline = loop.time() + cleanup_observe
            while not done.is_set() and loop.time() < cleanup_deadline:
                try:
                    await asyncio.sleep(_WORKER_POLL_SECONDS)
                except asyncio.CancelledError:
                    continue
            if not done.is_set() or state.get("cleanup_ok") is False:
                self.cleanup_quiesced = False
            raise

        if not completed:
            cleanup_observe = (
                _OWNED_PROCESS_CLEANUP_OBSERVE_SECONDS
                if "pid" in state
                else _CALLER_CLEANUP_OBSERVE_SECONDS
            )
            cleanup_deadline = loop.time() + cleanup_observe
            while not done.is_set() and loop.time() < cleanup_deadline:
                await asyncio.sleep(
                    min(
                        _WORKER_POLL_SECONDS,
                        max(0.0, cleanup_deadline - loop.time()),
                    )
                )
            if not done.is_set():
                self.cleanup_quiesced = False
                logger.warning(
                    "hook worker exceeded its cleanup bound: %s",
                    spec.command,
                )
                return

        status = state.get("status")
        if status == "completed":
            if state.get("returncode") != 0:
                logger.warning(
                    "hook command exited %s: %s",
                    state.get("returncode"),
                    spec.command,
                )
            return
        if status == "timeout":
            if state.get("cleanup_ok") is False:
                self.cleanup_quiesced = False
            logger.warning("hook command timed out after %.1fs: %s", timeout, spec.command)
            return
        if status == "spawn_error":
            logger.warning("hook command failed to start (%s): %s", spec.command, state.get("error"))
            return
        if status == "transport_error":
            if state.get("cleanup_ok") is False:
                self.cleanup_quiesced = False
            logger.warning("hook command transport failed (%s): %s", spec.command, state.get("error"))
            return
        if status == "cancelled":
            if state.get("cleanup_ok") is False:
                self.cleanup_quiesced = False
            logger.warning("hook command was abandoned and cleaned up: %s", spec.command)
            return
        if status == "cleanup_failed":
            self.cleanup_quiesced = False
            logger.warning(
                "hook command descendants did not quiesce after leader exit: %s",
                spec.command,
            )


__all__ = ["ShellHookRunner"]
