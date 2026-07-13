from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_SUPERVISOR = (
    Path(__file__).resolve().parents[2]
    / "swebench"
    / "openhands_process_supervisor.py"
)
_SWEBENCH_DIR = _SUPERVISOR.parent
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import openhands_process_supervisor as supervisor  # noqa: E402


def test_supervisor_decodes_wait_status_without_python39_helper(monkeypatch):
    monkeypatch.setattr(supervisor.os, "waitstatus_to_exitcode", None)

    assert supervisor._decode_wait_status(7 << 8) == 7
    assert supervisor._decode_wait_status(signal.SIGTERM) == -signal.SIGTERM


def test_container_final_empty_verification_rejects_late_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = iter(({}, {41: "late-start"}))
    clock = iter((0.0, 0.0, 0.1, 0.21))
    monkeypatch.setattr(
        supervisor,
        "_live_container_processes",
        lambda preserved: next(scans),
    )
    monkeypatch.setattr(
        supervisor,
        "_signal_container_processes",
        lambda identities, sig: None,
    )
    monkeypatch.setattr(supervisor, "_reap_adopted", lambda: None)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: None)

    with pytest.raises(supervisor.SupervisorError, match="remained after SIGKILL"):
        supervisor._require_stable_container_empty({1, 99})


def test_supervisor_converts_unexpected_errors_to_technical_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cleaned: list[int] = []

    def fail(*args, **kwargs):
        raise OSError("synthetic supervisor failure")

    monkeypatch.setattr(supervisor, "run", fail)
    monkeypatch.setattr(
        supervisor,
        "terminate_descendants",
        lambda pid: cleaned.append(pid),
    )

    assert supervisor.main(["--", "ignored-command"]) == 125
    assert cleaned == [supervisor.os.getpid()]
    assert "synthetic supervisor failure" in capsys.readouterr().err


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux subreaper regression",
)
def test_supervisor_captures_double_fork_setsid_escape(tmp_path: Path) -> None:
    sentinel = tmp_path / "setsid-leak"
    code = textwrap.dedent(
        f"""
        import os
        import pathlib
        import time

        if os.fork():
            os._exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        os.close(1)
        os.close(2)
        time.sleep(0.4)
        pathlib.Path({str(sentinel)!r}).write_text("leaked")
        os._exit(0)
        """
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_SUPERVISOR),
            "--",
            sys.executable,
            "-c",
            code,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux subreaper regression",
)
def test_supervisor_timeout_cleans_setsid_escape_before_return(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "timeout-setsid-leak"
    code = textwrap.dedent(
        f"""
        import os
        import pathlib
        import signal
        import time

        if os.fork():
            os._exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.close(1)
        os.close(2)
        time.sleep(0.4)
        pathlib.Path({str(sentinel)!r}).write_text("leaked")
        os._exit(0)
        """
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_SUPERVISOR),
            "--timeout-seconds",
            "0.05",
            "--",
            sys.executable,
            "-c",
            code,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="Unsupported-platform fail-closed regression",
)
def test_supervisor_fails_closed_without_linux_proc() -> None:
    result = subprocess.run(
        [sys.executable, str(_SUPERVISOR), "--", sys.executable, "-c", "pass"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 125
    assert "Linux /proc is required" in result.stderr
