"""Cancel actual preflight child writers before the caller releases its workspace."""

import json
import os
import sys
import threading
import time
from pathlib import Path
from threading import Event

import pytest
from arc_light import delivery

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Controlled POSIX executable fixtures")


CHILD = """
import json
import os
import time
from pathlib import Path

name = Path(__file__).name
args = __import__('sys').argv[1:]
phase = 'install' if name == 'npm' and args[:1] == ['install'] else 'health' if name == 'npm' else 'browser'
if phase == os.environ['ARC_PREFLIGHT_TEST_PHASE']:
    Path(os.environ['ARC_PREFLIGHT_TEST_HEARTBEAT']).write_text('tick\\n')
    ready = Path(os.environ['ARC_PREFLIGHT_TEST_READY'])
    temporary = ready.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'cwd': str(Path.cwd())}))
    temporary.replace(ready)
    while True:
        with Path(os.environ['ARC_PREFLIGHT_TEST_HEARTBEAT']).open('a') as stream:
            stream.write('tick\\n')
        time.sleep(.02)
"""


def controlled_preflight(tmp_path, monkeypatch, phase):
    workspace = tmp_path / "application"
    for name in ("backend", "frontend"):
        directory = workspace / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"scripts": {"start": "controlled child"}}))
    (workspace / "backend/source.txt").write_text("candidate source")
    delivery.capture_baseline(workspace)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("node", "npm"):
        script = commands / name
        script.write_text(f"#!{sys.executable}\n" + CHILD)
        script.chmod(0o755)
    ready = tmp_path / "child-ready.json"
    heartbeat = tmp_path / "heartbeat.txt"
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("ARC_PREFLIGHT_TEST_PHASE", phase)
    monkeypatch.setenv("ARC_PREFLIGHT_TEST_READY", str(ready))
    monkeypatch.setenv("ARC_PREFLIGHT_TEST_HEARTBEAT", str(heartbeat))
    # The child deliberately never becomes healthy. Port zero avoids allocating a server socket.
    monkeypatch.setattr(delivery, "_port", lambda: 0)
    return workspace, ready, heartbeat


@pytest.mark.parametrize("phase", ["install", "browser", "health"])
def test_cancelled_preflight_waits_for_real_writer_exit(tmp_path, monkeypatch, phase):
    workspace, ready, heartbeat = controlled_preflight(tmp_path, monkeypatch, phase)
    cancelled = Event()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(delivery.preflight(workspace, timeout=20, cancel_event=cancelled))
    )
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "The actual preflight subprocess did not start"
        child = json.loads(ready.read_text())
        cancelled.set()
        thread.join(10)
        assert not thread.is_alive(), "Preflight returned workspace ownership with a child still active"
        assert results and results[0]["cancelled"] and results[0]["fatal"] and not results[0]["ok"]
        assert results[0]["checks"][-1]["step"] == "preflight_error"
        with pytest.raises(ProcessLookupError):
            os.kill(child["pid"], 0)
        size = heartbeat.stat().st_size
        time.sleep(0.1)
        assert heartbeat.stat().st_size == size
        assert (workspace / "backend/source.txt").read_text() == "candidate source"
        assert Path(child["cwd"]) != workspace / "backend"
        assert not Path(child["cwd"]).parent.exists()
        saved = json.loads((workspace / ".arc/checks/preflight.json").read_text())
        assert saved["cancelled"] and not saved["ok"]
    finally:
        cancelled.set()
        thread.join(25)


def test_precancelled_preflight_starts_no_process(tmp_path, monkeypatch):
    workspace, ready, _ = controlled_preflight(tmp_path, monkeypatch, "install")
    cancelled = Event()
    cancelled.set()
    monkeypatch.setattr(
        delivery.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Cancelled process started")
    )
    report = delivery.preflight(workspace, cancel_event=cancelled)
    assert report["cancelled"] and report["fatal"] and not ready.exists()


def test_default_preflight_keeps_repairable_startup_failure(tmp_path, monkeypatch):
    workspace, ready, _ = controlled_preflight(tmp_path, monkeypatch, "none")
    report = delivery.preflight(workspace, timeout=20)
    assert not report["ok"] and not report["fatal"] and "cancelled" not in report
    assert next(check for check in report["checks"] if check["step"] == "browser_launch")["ok"]
    assert not next(check for check in report["checks"] if check["step"] == "backend_health")["ok"]
    assert not ready.exists()
