"""Exercise the independent checker entry and current input failure reports."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from arc_light.delivery import capture_baseline

ENTRY = Path(__file__).resolve().parents[1] / "check.py"


def test_cli_missing_inputs_returns_failure_and_writes_current_report(tmp_path):
    output = tmp_path / ".arc/checks/verification.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"ok": true}')
    result = subprocess.run([sys.executable, str(ENTRY), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert not json.loads(result.stdout)["ok"]
    assert not json.loads(output.read_text())["ok"]


def test_cli_reinitialization_keeps_original_snapshot_and_reports_input_error(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    with sqlite3.connect(backend / "app.sqlite") as db:
        db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO records VALUES(1)")
    capture_baseline(tmp_path)
    original = (tmp_path / ".arc/checks/evolution-baseline.json").read_bytes()
    with sqlite3.connect(backend / "app.sqlite") as db:
        db.execute("DELETE FROM records")
    result = subprocess.run([sys.executable, str(ENTRY), str(tmp_path), "--initialize"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "resume" in json.loads(result.stdout)["input_error"]
    assert (tmp_path / ".arc/checks/evolution-baseline.json").read_bytes() == original
    assert not json.loads((tmp_path / ".arc/checks/verification.json").read_text())["ok"]
