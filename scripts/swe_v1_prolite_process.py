"""Bounded local and remote process cleanup for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from scripts.swe_v1_prolite_common import (
    LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
    LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
    LOCAL_PROCESS_TERM_GRACE_SECONDS,
    LOCAL_SPAWN_SIGNALS,
    MAX_REMOTE_OUTPUT_TAIL_CHARS,
    REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS,
    _redacted,
)


def terminate_remote_run(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    runtime_repo = remote_runtime_repo or str(Path(base_run_dir) / "_runtime" / "repo")
    remote_pythonpath = str(Path(runtime_repo) / "opencollab")
    remote_command = (
        "env PYTHONPATH="
        + shlex.quote(remote_pythonpath)
        + " python3 -m opencollab.harness.swe_v1_remote_cleanup "
        + shlex.quote(base_run_dir)
    )
    result = subprocess.run(
        [*ssh_command, host, remote_command],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        detail = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = {
            "stdout": _redacted(result.stdout),
            "stderr": _redacted(result.stderr),
        }
    return {"returncode": result.returncode, "detail": detail}


def _wait_for_owned_local_cleanup(
    done: threading.Event,
    *,
    timeout: float,
) -> tuple[bool, BaseException | None]:
    deadline = time.monotonic() + max(0.0, timeout)
    interruption: BaseException | None = None
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            done.wait(min(0.05, remaining))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
    return done.is_set(), interruption


def _block_local_spawn_signals() -> dict[str, object]:
    state: dict[str, object] = {
        "previous": {},
        "pending": [],
        "restored": False,
    }

    def defer(signum: int, _frame: object) -> None:
        pending = state["pending"]
        if isinstance(pending, list) and signum not in pending:
            pending.append(signum)

    previous: dict[signal.Signals, Any] = {}
    state["previous"] = previous
    try:
        for signum in LOCAL_SPAWN_SIGNALS:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, defer)
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    return state


def _restore_local_spawn_signals(
    state: dict[str, object],
) -> None:
    if state.get("restored"):
        return
    previous = state.get("previous")
    if not isinstance(previous, dict):
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)
    state["restored"] = True
    pending = state.get("pending")
    for signum in pending if isinstance(pending, list) else []:
        handler = previous.get(signum, signal.SIG_DFL)
        if handler == signal.SIG_IGN:
            continue
        if handler == signal.SIG_DFL:
            os.kill(os.getpid(), signum)
        else:
            handler(signum, None)


class _BoundedTextTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = ""
        self.total_chars = 0
        self.lock = threading.Lock()

    def append(self, chunk: str) -> None:
        with self.lock:
            self.total_chars += len(chunk)
            self.value = (self.value + chunk)[-self.limit :]

    def render(self) -> str:
        with self.lock:
            if self.total_chars <= self.limit:
                return self.value
            omitted = self.total_chars - len(self.value)
            return f"[truncated {omitted} chars]\n{self.value}"


def _drain_text_stream(stream: Any, sink: _BoundedTextTail) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def _bounded_remote_communicate(
    proc: subprocess.Popen[str],
    input_text: str,
    *,
    timeout: float,
) -> tuple[str, str]:
    if (
        getattr(proc, "stdout", None) is None
        or getattr(proc, "stderr", None) is None
        or getattr(proc, "stdin", None) is None
    ):
        return proc.communicate(input_text, timeout=timeout)
    stdout_tail = _BoundedTextTail(MAX_REMOTE_OUTPUT_TAIL_CHARS)
    stderr_tail = _BoundedTextTail(MAX_REMOTE_OUTPUT_TAIL_CHARS)
    threads = [
        threading.Thread(
            target=_drain_text_stream,
            args=(proc.stdout, stdout_tail),
            name=f"prolite-stdout-{proc.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_text_stream,
            args=(proc.stderr, stderr_tail),
            name=f"prolite-stderr-{proc.pid}",
            daemon=True,
        ),
    ]
    setattr(proc, "_opencollab_bounded_drainers", threads)
    for thread in threads:
        thread.start()
    try:
        proc.stdin.write(input_text)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    proc.wait(timeout=timeout)
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("remote output drain did not reach EOF")
    return stdout_tail.render(), stderr_tail.render()


def _wait_or_communicate_local_process(
    proc: subprocess.Popen[str],
    *,
    timeout: float | None = None,
) -> None:
    if getattr(proc, "_opencollab_bounded_drainers", None) is not None:
        proc.wait(timeout=timeout)
    else:
        proc.communicate(timeout=timeout)


def _consume_local_process_exit(proc: subprocess.Popen[str]) -> None:
    try:
        _wait_or_communicate_local_process(proc)
    except BaseException:
        pass


def _schedule_local_process_exit_consumer(proc: subprocess.Popen[str]) -> None:
    threading.Thread(
        target=_consume_local_process_exit,
        args=(proc,),
        name=f"prolite-local-reap-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def _local_process_group_exists(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_local_process_group_exit(pgid: int, *, deadline: float) -> bool:
    while _local_process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _terminate_local_process_group_owned(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float,
    kill_timeout: float,
) -> bool:
    pgid = proc.pid
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            _schedule_local_process_exit_consumer(proc)
            return False

    leader_reaped = False
    try:
        _wait_or_communicate_local_process(
            proc,
            timeout=max(0.0, term_deadline - time.monotonic()),
        )
        leader_reaped = True
    except ChildProcessError:
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass

    group_gone = _wait_for_local_process_group_exit(pgid, deadline=term_deadline)
    if leader_reaped and group_gone:
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    if not leader_reaped:
        try:
            _wait_or_communicate_local_process(
                proc,
                timeout=max(0.0, kill_deadline - time.monotonic()),
            )
            leader_reaped = True
        except ChildProcessError:
            leader_reaped = True
        except subprocess.TimeoutExpired:
            pass
    group_gone = _wait_for_local_process_group_exit(pgid, deadline=kill_deadline)
    if not leader_reaped:
        _schedule_local_process_exit_consumer(proc)
    return leader_reaped and group_gone


def terminate_local_process_group(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float = LOCAL_PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
) -> bool:
    """Terminate an SSH wrapper and drain its pipes without an unbounded wait."""
    state: dict[str, object] = {}
    done = threading.Event()

    def cleanup() -> None:
        try:
            state["reaped"] = _terminate_local_process_group_owned(
                proc,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
            )
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    cleanup_thread = threading.Thread(
        target=cleanup,
        name=f"prolite-local-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    cleanup_thread.start()
    completed, interruption = _wait_for_owned_local_cleanup(
        done,
        timeout=(term_timeout + kill_timeout + LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS),
    )
    if completed and "reaped" in state:
        reaped = bool(state["reaped"])
    else:
        reaped = False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
    if interruption is not None:
        raise interruption
    return reaped


def _ensure_local_process_group_quiesced_after_wait(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float = LOCAL_PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
) -> bool:
    if not _local_process_group_exists(proc.pid):
        return True
    return terminate_local_process_group(
        proc,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def _cleanup_remote_execution(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str | None = None,
    proc: subprocess.Popen[str],
) -> tuple[dict[str, Any], BaseException | None]:
    """Run remote and local cleanup under one caller-interrupt-resistant owner."""
    state: dict[str, object] = {}
    done = threading.Event()

    def cleanup() -> None:
        remote_state: dict[str, object] = {}
        remote_done = threading.Event()

        def cleanup_remote() -> None:
            try:
                remote_state["result"] = terminate_remote_run(
                    ssh_command=ssh_command,
                    host=host,
                    base_run_dir=base_run_dir,
                    remote_runtime_repo=remote_runtime_repo,
                    timeout=int(REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS),
                )
            except BaseException as exc:
                remote_state["result"] = {
                    "returncode": 125,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                remote_done.set()

        try:
            threading.Thread(
                target=cleanup_remote,
                name=f"prolite-remote-command-cleanup-{getattr(proc, 'pid', 'unknown')}",
                daemon=True,
            ).start()
            try:
                local_quiesced = terminate_local_process_group(proc)
            except BaseException as exc:
                local_quiesced = False
                state["local_error"] = f"{type(exc).__name__}: {exc}"
            remote_completed = remote_done.wait(REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS + 1.0)
            remote = remote_state.get("result")
            if not remote_completed or not isinstance(remote, dict):
                remote = {
                    "returncode": 125,
                    "error": "remote cleanup exceeded its outer bound",
                }
            state["result"] = {
                "ok": remote.get("returncode") == 0 and local_quiesced,
                "remote": remote,
                "local_cleanup_quiesced": local_quiesced,
                "completed": True,
            }
        finally:
            done.set()

    threading.Thread(
        target=cleanup,
        name=f"prolite-remote-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()
    completed, interruption = _wait_for_owned_local_cleanup(
        done,
        timeout=(
            max(
                REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS + 1.0,
                LOCAL_PROCESS_TERM_GRACE_SECONDS
                + LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS
                + LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
            )
            + 1.0
        ),
    )
    if completed and isinstance(state.get("result"), dict):
        result = dict(state["result"])
        if "local_error" in state:
            result["local_error"] = state["local_error"]
        return result, interruption

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    _schedule_local_process_exit_consumer(proc)
    return {
        "ok": False,
        "remote": state.get("result", {}).get("remote")
        if isinstance(state.get("result"), dict)
        else {"returncode": 125, "error": "cleanup exceeded outer bound"},
        "local_cleanup_quiesced": False,
        "completed": False,
    }, interruption


__all__ = [name for name in globals() if not name.startswith("__")]
