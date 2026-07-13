"""Host-trusted container process supervision shared by SWE generators."""

from __future__ import annotations

import functools
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SWEBENCH_DIR = REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

from gen_prediction_config import _docker_timeout_from_env  # noqa: E402
from openhands_process_supervisor import _proc_identity  # noqa: E402

CONTAINER_GUARD_ROOT = "/tmp/opencollab-openhands-processes"
_CONTAINER_GUARD_SOURCE = REPO_ROOT / "scripts" / "container_process_guard.py"
_CONTAINER_SUPERVISOR_SOURCE = REPO_ROOT / "swebench" / "openhands_process_supervisor.py"


class HostPidNamespaceUnavailable(RuntimeError):
    """Raised when Docker daemon PIDs cannot be controlled from this host."""


@dataclass(frozen=True)
class GuardedTerminalInvocation:
    argv: tuple[str, ...]
    source: str
    pidfile: str
    cancelfile: str


@functools.lru_cache(maxsize=1)
def container_process_guard_source() -> str:
    source = _CONTAINER_GUARD_SOURCE.read_text(encoding="utf-8")
    if not source.startswith("#!/usr/bin/env python3\n"):
        raise RuntimeError("container process guard source is missing or invalid")
    return source


@functools.lru_cache(maxsize=1)
def trusted_supervisor_source() -> str:
    source = _CONTAINER_SUPERVISOR_SOURCE.read_text(encoding="utf-8")
    if not source.startswith('"""Linux subreaper wrapper'):
        raise RuntimeError("container supervisor source is missing or invalid")
    return source


def container_control_timeout() -> float:
    return max(float(_docker_timeout_from_env()), 10.0)


def _run_trusted_helper(
    container_id: str,
    python_bin: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_id,
            python_bin,
            "-I",
            "-S",
            "-",
            *arguments,
        ],
        input=trusted_supervisor_source(),
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )


def probe_container_python(container_id: str) -> str:
    try:
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-c",
                "command -v python3 || command -v python",
            ],
            capture_output=True,
            text=True,
            timeout=container_control_timeout(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"container Python probe failed: {exc}") from exc
    python_bin = probe.stdout.strip()
    if (
        probe.returncode != 0
        or not python_bin.startswith("/")
        or any(character.isspace() for character in python_bin)
    ):
        detail = (probe.stderr or probe.stdout).strip()
        raise RuntimeError(
            "container supervision requires an absolute Python executable"
            f" (exit {probe.returncode}): {detail}"
        )
    return python_bin


def prepare_container_guard(container_id: str) -> str:
    python_bin = probe_container_python(container_id)
    prepared = _run_trusted_helper(
        container_id,
        python_bin,
        "--prepare-guard-root",
        CONTAINER_GUARD_ROOT,
    )
    if prepared.returncode != 0:
        detail = (prepared.stderr or prepared.stdout).strip()
        raise RuntimeError(
            "container process guard preparation failed"
            f" (exit {prepared.returncode}): {detail}"
        )
    return python_bin


def quiesce_container(
    container_id: str,
    python_bin: str | None = None,
    *,
    cleanup_guard_root: bool = False,
) -> dict[str, object]:
    del python_bin, cleanup_guard_root
    try:
        _quiesce_from_host(container_id)
    except HostPidNamespaceUnavailable:
        try:
            _quiesce_with_daemon_helper(container_id)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {
                "proven": False,
                "returncode": 125,
                "error": f"{type(exc).__name__}: {exc}",
            }
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {
            "proven": False,
            "returncode": 125,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "proven": True,
        "returncode": 0,
        "error": "",
    }


def _container_init_pid(container_id: str) -> int:
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.State.Running}} {{.State.Pid}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )
    fields = inspected.stdout.split()
    if inspected.returncode != 0 or len(fields) != 2 or fields[0] != "true":
        detail = (inspected.stderr or inspected.stdout).strip()
        raise RuntimeError(
            "Docker did not prove the container running"
            f" (exit {inspected.returncode}): {detail}"
        )
    try:
        init_pid = int(fields[1])
    except ValueError as exc:
        raise RuntimeError("Docker returned an invalid container init pid") from exc
    if init_pid <= 1:
        raise RuntimeError("Docker returned an invalid container init pid")
    return init_pid


def _docker_top_pids(container_id: str, init_pid: int) -> set[int]:
    top = subprocess.run(
        ["docker", "top", container_id, "-eo", "pid,stat"],
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )
    if top.returncode != 0:
        detail = (top.stderr or top.stdout).strip()
        raise RuntimeError(f"docker top failed (exit {top.returncode}): {detail}")
    states: dict[int, str] = {}
    for line in top.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        fields = value.split()
        if [field.upper() for field in fields] == ["PID", "STAT"]:
            continue
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1]:
            raise RuntimeError(f"docker top returned malformed process data: {value!r}")
        states[int(fields[0])] = fields[1]
    if init_pid not in states or states[init_pid].startswith("Z"):
        raise RuntimeError("docker top omitted the inspected container init pid")
    return {pid for pid, state in states.items() if not state.startswith("Z")}


def _container_process_identities(container_id: str, init_pid: int) -> dict[int, str]:
    identities: dict[int, str] = {}
    for pid in _docker_top_pids(container_id, init_pid):
        if pid == init_pid:
            continue
        try:
            identity = _proc_identity(pid)
        except RuntimeError as exc:
            raise HostPidNamespaceUnavailable(
                f"cannot inspect container host pid {pid}: {exc}"
            ) from exc
        if identity is None:
            continue
        if identity[1] != "Z":
            identities[pid] = identity[2]
    return identities


def _signal_host_identities(
    identities: dict[int, str],
    sig: signal.Signals,
) -> None:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise HostPidNamespaceUnavailable("host pidfd signalling is unavailable")
    for pid, expected_start in identities.items():
        try:
            pidfd = pidfd_open(pid, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise HostPidNamespaceUnavailable(
                f"cannot open pidfd for container host pid {pid}: {exc}"
            ) from exc
        try:
            try:
                current = _proc_identity(pid)
            except RuntimeError as exc:
                raise HostPidNamespaceUnavailable(
                    f"cannot recheck container host pid {pid}: {exc}"
                ) from exc
            if current is None or current[1] == "Z" or current[2] != expected_start:
                continue
            try:
                pidfd_send_signal(pidfd, sig)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise HostPidNamespaceUnavailable(
                    f"cannot signal container host pid {pid}: {exc}"
                ) from exc
            except OSError as exc:
                raise HostPidNamespaceUnavailable(
                    f"pidfd signalling failed for container host pid {pid}: {exc}"
                ) from exc
        finally:
            os.close(pidfd)


def _quiesce_from_host(container_id: str) -> None:
    init_pid = _container_init_pid(container_id)
    try:
        init_identity = _proc_identity(init_pid)
    except RuntimeError as exc:
        raise HostPidNamespaceUnavailable(
            f"cannot inspect container init pid {init_pid}: {exc}"
        ) from exc
    if init_identity is None or init_identity[1] == "Z":
        raise HostPidNamespaceUnavailable(
            "container init pid is not live in the host /proc namespace"
        )
    for sig, timeout in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + timeout
        empty_scans = 0
        while time.monotonic() < deadline:
            live = _container_process_identities(container_id, init_pid)
            if not live:
                empty_scans += 1
                if empty_scans >= 2:
                    return
            else:
                empty_scans = 0
                _signal_host_identities(live, sig)
            time.sleep(0.05)
    _require_stable_host_empty(container_id, init_pid)


def _require_stable_host_empty(
    container_id: str,
    init_pid: int,
    *,
    timeout: float = 0.2,
) -> None:
    deadline = time.monotonic() + timeout
    empty_scans = 0
    last_live: dict[int, str] = {}
    while time.monotonic() < deadline:
        last_live = _container_process_identities(container_id, init_pid)
        if not last_live:
            empty_scans += 1
            if empty_scans >= 2:
                return
        else:
            empty_scans = 0
            _signal_host_identities(last_live, signal.SIGKILL)
        time.sleep(0.05)
    if last_live:
        raise RuntimeError(
            "container host processes remained after SIGKILL: "
            f"{sorted(last_live)[:20]}"
        )
    raise RuntimeError("container host process emptiness was not observed twice")


def _container_image_id(container_id: str) -> str:
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.Image}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        detail = (inspected.stderr or inspected.stdout).strip()
        raise RuntimeError(
            "Docker did not return an immutable container image id"
            f" (exit {inspected.returncode}): {detail}"
        )
    return image_id


def _fresh_image_python(image_id: str) -> str:
    probed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            "command -v python3 || command -v python",
        ],
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )
    python_bin = probed.stdout.strip()
    if (
        probed.returncode != 0
        or not python_bin.startswith("/")
        or any(character.isspace() for character in python_bin)
    ):
        detail = (probed.stderr or probed.stdout).strip()
        raise RuntimeError(
            "fresh helper image lacks an absolute Python executable"
            f" (exit {probed.returncode}): {detail}"
        )
    return python_bin


def _remove_helper_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
        timeout=container_control_timeout(),
        check=False,
    )


def _quiesce_with_daemon_helper(container_id: str) -> None:
    image_id = _container_image_id(container_id)
    python_bin = _fresh_image_python(image_id)
    helper_name = f"oc-quiesce-{uuid.uuid4().hex}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        helper_name,
        "--pid",
        f"container:{container_id}",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "KILL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        python_bin,
        image_id,
        "-I",
        "-S",
        "-",
        "--quiesce-container",
    ]
    try:
        result = subprocess.run(
            command,
            input=trusted_supervisor_source(),
            capture_output=True,
            text=True,
            timeout=container_control_timeout(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _remove_helper_container(helper_name)
        raise
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "daemon-side container quiescer failed"
            f" (exit {result.returncode}): {detail}"
        )
    init_pid = _container_init_pid(container_id)
    remaining = _docker_top_pids(container_id, init_pid) - {init_pid}
    if remaining:
        raise RuntimeError(
            "docker top found processes after daemon-side quiescence: "
            f"{sorted(remaining)[:20]}"
        )


def require_container_quiescence(container_id: str) -> None:
    evidence = quiesce_container(container_id)
    if evidence["proven"] is True:
        return
    raise RuntimeError(
        "container process quiescence could not be proven"
        f" (exit {evidence['returncode']}): {evidence['error']}"
    )


def _guard_settings_from_env() -> tuple[str, str]:
    python_bin = os.environ.get("OPENHANDS_CONTAINER_PYTHON", "").strip()
    guard_root = os.environ.get("OPENHANDS_CONTAINER_GUARD_ROOT", "").strip()
    if not python_bin.startswith("/") or any(character.isspace() for character in python_bin):
        raise RuntimeError("OpenHands container guard requires an absolute Python executable")
    if guard_root != CONTAINER_GUARD_ROOT:
        raise RuntimeError("OpenHands container guard root is missing or invalid")
    return python_bin, guard_root


def guarded_terminal_invocation(
    container_id: str,
    command: str,
    *,
    session_id: str | None = None,
) -> GuardedTerminalInvocation:
    python_bin, guard_root = _guard_settings_from_env()
    session_id = session_id or uuid.uuid4().hex
    if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
        raise RuntimeError("OpenHands terminal guard session id is invalid")
    pidfile = f"{guard_root}/{session_id}.pid"
    cancelfile = f"{pidfile}.cancel"
    return GuardedTerminalInvocation(
        argv=(
            "docker",
            "exec",
            "-i",
            container_id,
            python_bin,
            "-I",
            "-S",
            "-",
            "run",
            pidfile,
            cancelfile,
            "bash",
            "-lc",
            'cd -- "$1" && eval "$2"',
            "opencollab-shell",
            "/testbed",
            command,
        ),
        source=container_process_guard_source(),
        pidfile=pidfile,
        cancelfile=cancelfile,
    )


def stop_guarded_terminal_session(
    container_id: str,
    invocation: GuardedTerminalInvocation,
) -> subprocess.CompletedProcess[str]:
    python_bin, _guard_root = _guard_settings_from_env()
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_id,
            python_bin,
            "-I",
            "-S",
            "-",
            "stop",
            invocation.pidfile,
            invocation.cancelfile,
        ],
        input=invocation.source,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: container_quiescence.py <container-id>", file=sys.stderr)
        raise SystemExit(125)
    try:
        require_container_quiescence(sys.argv[1])
    except Exception as exc:
        print(f"container quiescence technical failure: {exc}", file=sys.stderr)
        raise SystemExit(125) from exc
