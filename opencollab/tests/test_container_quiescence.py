from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from swebench import container_quiescence as guard  # noqa: E402
from swebench import openhands_process_supervisor as supervisor  # noqa: E402


def test_host_quiescer_rechecks_process_churn_until_twice_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = iter(
        [
            {41: "start-41"},
            {42: "start-42"},
            {},
            {},
        ]
    )
    signalled: list[tuple[dict[int, str], signal.Signals]] = []
    monkeypatch.setattr(guard, "_container_init_pid", lambda container_id: 30)
    monkeypatch.setattr(guard, "_proc_identity", lambda pid: (0, "S", "init-start"))
    monkeypatch.setattr(
        guard,
        "_container_process_identities",
        lambda container_id, init_pid: next(scans),
    )
    monkeypatch.setattr(
        guard,
        "_signal_host_identities",
        lambda identities, sig: signalled.append((identities, sig)),
    )
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: None)

    guard._quiesce_from_host("container-123")

    assert signalled == [
        ({41: "start-41"}, signal.SIGTERM),
        ({42: "start-42"}, signal.SIGTERM),
    ]


def test_host_quiescer_does_not_signal_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        guard,
        "_proc_identity",
        lambda pid: (1, "S", "new-start"),
    )
    monkeypatch.setattr(
        guard.os,
        "pidfd_open",
        lambda pid, flags: 7,
        raising=False,
    )
    monkeypatch.setattr(
        guard.signal,
        "pidfd_send_signal",
        lambda pidfd, sig: sent.append((pidfd, sig)),
        raising=False,
    )
    monkeypatch.setattr(guard.os, "close", lambda fd: None)

    guard._signal_host_identities({41: "old-start"}, signal.SIGKILL)

    assert sent == []


def test_docker_top_process_that_exits_before_proc_read_is_rescanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard, "_docker_top_pids", lambda container_id, init_pid: {30, 41})
    monkeypatch.setattr(guard, "_proc_identity", lambda pid: None)

    assert guard._container_process_identities("container-123", 30) == {}


def test_docker_top_ignores_zombies_but_keeps_live_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="PID STAT\n30 S\n41 Z\n42 Z+\n43 R\n",
            stderr="",
        ),
    )

    assert guard._docker_top_pids("container-123", 30) == {30, 43}


def test_final_empty_verification_rejects_single_empty_scan_then_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = iter(({}, {41: "late-start"}))
    clock = iter((0.0, 0.0, 0.1, 0.21))
    monkeypatch.setattr(
        guard,
        "_container_process_identities",
        lambda container_id, init_pid: next(scans),
    )
    monkeypatch.setattr(guard, "_signal_host_identities", lambda identities, sig: None)
    monkeypatch.setattr(guard.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="remained after SIGKILL"):
        guard._require_stable_host_empty("container-123", 30)


def test_invisible_host_pid_namespace_uses_daemon_side_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unavailable(container_id):
        raise guard.HostPidNamespaceUnavailable("daemon VM pid namespace")

    monkeypatch.setattr(
        guard,
        "_quiesce_from_host",
        unavailable,
    )
    monkeypatch.setattr(
        guard,
        "_quiesce_with_daemon_helper",
        lambda container_id: calls.append(container_id),
    )

    evidence = guard.quiesce_container("container-123")

    assert evidence["proven"] is True
    assert calls == ["container-123"]


def test_daemon_helper_exit_zero_still_requires_empty_docker_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    captured: dict = {}
    monkeypatch.setattr(guard, "_container_image_id", lambda container_id: image_id)
    monkeypatch.setattr(guard, "_fresh_image_python", lambda image: "/usr/bin/python3")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        guard.subprocess,
        "run",
        fake_run,
    )
    monkeypatch.setattr(guard, "_container_init_pid", lambda container_id: 30)
    monkeypatch.setattr(
        guard,
        "_docker_top_pids",
        lambda container_id, init_pid: {30, 41},
    )

    with pytest.raises(RuntimeError, match="docker top found processes"):
        guard._quiesce_with_daemon_helper("container-123")

    assert "--pid" in captured["command"]
    assert "container:container-123" in captured["command"]
    assert "--network" in captured["command"]
    assert "none" in captured["command"]
    assert "--read-only" in captured["command"]
    assert image_id in captured["command"]
    assert captured["command"][-4:] == ["-I", "-S", "-", "--quiesce-container"]
    assert "def quiesce_container()" in captured["input"]


def test_forged_container_helper_success_cannot_override_docker_top_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard, "probe_container_python", lambda container_id: "/usr/bin/python3")
    monkeypatch.setattr(
        guard,
        "_run_trusted_helper",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert guard.prepare_container_guard("container-123") == "/usr/bin/python3"

    clock = 0.0

    def advance_clock():
        nonlocal clock
        clock += 0.11
        return clock

    monkeypatch.setattr(guard, "_container_init_pid", lambda container_id: 30)
    monkeypatch.setattr(guard, "_proc_identity", lambda pid: (0, "S", "init-start"))
    monkeypatch.setattr(
        guard,
        "_container_process_identities",
        lambda container_id, init_pid: {41: "escaped-start"},
    )
    monkeypatch.setattr(guard, "_signal_host_identities", lambda identities, sig: None)
    monkeypatch.setattr(guard.time, "monotonic", advance_clock)
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: None)

    evidence = guard.quiesce_container("container-123", "/usr/bin/python3")

    assert evidence["proven"] is False
    assert evidence["returncode"] == 125
    assert "remained after SIGKILL" in str(evidence["error"])


def test_guard_root_preparation_removes_only_owned_marker_shapes(tmp_path: Path) -> None:
    root = tmp_path / "guard"
    root.mkdir()
    for suffix in (".pid", ".pid.cancel", ".pid.lock"):
        (root / ("a" * 32 + suffix)).write_text("marker", encoding="utf-8")

    supervisor.prepare_guard_root(root)

    assert list(root.iterdir()) == []
    assert root.stat().st_mode & 0o777 == 0o700


def test_guard_root_preparation_rejects_unowned_entry(tmp_path: Path) -> None:
    root = tmp_path / "guard"
    root.mkdir()
    (root / "unexpected").write_text("keep", encoding="utf-8")

    with pytest.raises(supervisor.SupervisorError, match="unexpected guard-root entry"):
        supervisor.prepare_guard_root(root)

    assert (root / "unexpected").read_text(encoding="utf-8") == "keep"


def _docker_regression_image() -> str:
    image = os.environ.get("OPENCOLLAB_TEST_CONTAINER_IMAGE", "").strip()
    if not image:
        pytest.skip("OPENCOLLAB_TEST_CONTAINER_IMAGE is not configured")
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    return image


def test_quiescer_stops_double_fork_setsid_delayed_write() -> None:
    image = _docker_regression_image()
    created = subprocess.run(
        ["docker", "run", "-d", "--entrypoint", "", image, "tail", "-f", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    container_id = created.stdout.strip()
    try:
        python_probe = subprocess.run(
            ["docker", "exec", container_id, "sh", "-c", "command -v python3 || command -v python"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert python_probe.returncode == 0, python_probe.stderr
        python_bin = python_probe.stdout.strip()
        delayed = "/tmp/opencollab-delayed-write"
        ready = "/tmp/opencollab-escape-ready"
        code = textwrap.dedent(
            f"""
            import os
            import pathlib
            import signal
            import time

            pathlib.Path({ready!r}).touch()
            if os.fork():
                os._exit(0)
            os.setsid()
            if os.fork():
                os._exit(0)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(0.5)
            pathlib.Path({delayed!r}).write_text("escaped")
            os._exit(0)
            """
        )
        launched = subprocess.run(
            ["docker", "exec", "-d", container_id, python_bin, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert launched.returncode == 0, launched.stderr
        ready_probe = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-c",
                f"for i in 1 2 3 4 5; do [ -e {ready} ] && exit 0; sleep .1; done; exit 1",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert ready_probe.returncode == 0, ready_probe.stderr

        guard.require_container_quiescence(container_id)

        time.sleep(0.7)
        leaked = subprocess.run(
            ["docker", "exec", container_id, "test", "-e", delayed],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert leaked.returncode == 1
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
