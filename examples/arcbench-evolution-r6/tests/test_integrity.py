"""Execute preservation and cancellation against real SQLite databases."""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from threading import Event

import pytest
from arc_light.delivery import capture_baseline, isolated_application, load_baseline, run_command, verify
from arc_light.integrity import consistent_backup, database_values, working_integrity
from arc_light.public_checks import write_public_checks


def application(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    database = backend / "app.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO records VALUES (1, 'inherited')")
    (backend / "inherited.test.js").write_text("inherited assertion")
    return database


def test_resume_uses_original_rows_and_repeated_capture_is_refused(tmp_path):
    database = application(tmp_path)
    capture_baseline(tmp_path, run_id="existing-run", input_source="public-requirements.yaml")
    original = load_baseline(tmp_path, run_id="existing-run")
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM records")
    with pytest.raises(FileExistsError):
        capture_baseline(tmp_path)
    assert load_baseline(tmp_path, run_id="existing-run") == original
    failures = [c for c in working_integrity(tmp_path) if not c["ok"]]
    assert json.loads(failures[0]["detail"])[0]["removed_or_changed_rows"] == 1
    with isolated_application(tmp_path) as isolated:
        assert database_values(isolated / "backend/app.sqlite") == original["inherited_values"]["app.sqlite"]
    assert database_values(database)["records"]["rows"] == 0


def test_snapshot_missing_corrupt_or_wrong_run_stays_unverified(tmp_path):
    application(tmp_path)
    assert not working_integrity(tmp_path)[0]["ok"]
    capture_baseline(tmp_path, run_id="original")
    with pytest.raises(ValueError, match="different run"):
        load_baseline(tmp_path, run_id="new-run")
    (tmp_path / ".arc/checks/inherited-databases/0").write_bytes(b"damaged")
    with pytest.raises(sqlite3.Error):
        load_baseline(tmp_path)
    write_public_checks({"id": "REQ-1", "name": "Unknown", "scenarios": []}, tmp_path)
    report = verify(tmp_path)
    assert not report["ok"] and report["checks"][0]["step"] == "verification_inputs"


def test_wal_committed_data_enters_backup_and_protected_tests_survive(tmp_path):
    database = application(tmp_path)
    db = sqlite3.connect(database)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO records VALUES (2, 'committed WAL')")
        db.commit()
        assert Path(str(database) + "-wal").is_file()
        copy = tmp_path / "backup.sqlite"
        consistent_backup(database, copy)
        assert database_values(copy) == database_values(database)
        capture_baseline(tmp_path)
        (tmp_path / "backend/inherited.test.js").write_text("weakened assertion")
        assert not next(c for c in working_integrity(tmp_path) if c["step"] == "inherited_test_integrity")["ok"]
    finally:
        db.close()


def test_cancelled_verifier_rolls_back_real_sqlite_write_and_cleans_copy(tmp_path):
    database = application(tmp_path)
    capture_baseline(tmp_path)
    original = database_values(database)
    cancelled = Event()
    failures = []
    copies = []
    ready = tmp_path / "ready"
    script = (
        "import sqlite3,time,pathlib; "
        "db=sqlite3.connect('app.sqlite'); db.execute('BEGIN'); "
        "db.execute(\"UPDATE records SET value='unfinished'\"); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    )

    def execute():
        try:
            with isolated_application(tmp_path) as isolated:
                copies.append(isolated)
                run_command([sys.executable, "-c", script], isolated / "backend", 30, cancel_event=cancelled)
        except TimeoutError as exc:
            failures.append(str(exc))

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    cancelled.set()
    thread.join(10)
    assert ready.exists() and not thread.is_alive()
    assert failures == ["Verification cancelled; no acceptance verdict"]
    assert database_values(database) == original
    assert copies and not copies[0].exists()


def test_malformed_snapshot_returns_input_failure_and_keeps_candidate(tmp_path):
    database = application(tmp_path)
    capture_baseline(tmp_path)
    write_public_checks({"id": "REQ-1", "name": "Unknown", "scenarios": []}, tmp_path)
    (tmp_path / ".arc/checks/evolution-baseline.json").write_text("[]")
    before = database.read_bytes()
    report = verify(tmp_path)
    assert not report["ok"] and report["checks"][0]["step"] == "verification_inputs"
    assert database.read_bytes() == before
