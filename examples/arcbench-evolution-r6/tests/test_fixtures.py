"""Unknown schema capabilities remain explicit and unverified."""

import sqlite3

from arc_light.fixtures import verify_fixtures


def test_nested_database_verifies_published_inputs(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    with sqlite3.connect(directory / "app.sqlite") as db:
        db.executescript(
            "CREATE TABLE accounts(username TEXT,email TEXT);"
            "INSERT INTO accounts VALUES('alice','alice@example.test');"
            "CREATE TABLE workbooks(name TEXT);"
            "INSERT INTO workbooks VALUES('Public workbook');"
        )
    report = verify_fixtures(
        tmp_path,
        {
            "evolution_checks": [
                {
                    "source_ids": ["REQ-1"],
                    "scenario_index": 0,
                    "account": {"username": "alice", "email": "alice@example.test"},
                    "given": "workbook `Public workbook`",
                }
            ]
        },
    )
    assert report["ok"] and report["supported"]
    assert {check["kind"] for check in report["checks"]} == {"account", "workbook"}


def test_unknown_schema_cannot_verify_required_account_or_workbook(tmp_path):
    with sqlite3.connect(tmp_path / "unknown.sqlite") as db:
        db.execute("CREATE TABLE arbitrary(id INTEGER)")
    report = verify_fixtures(
        tmp_path,
        {
            "evolution_checks": [
                {
                    "source_ids": ["REQ-1"],
                    "scenario_index": 0,
                    "account": {"username": "alice", "email": "alice@example.test"},
                    "given": "workbook `Public workbook`",
                }
            ]
        },
    )
    assert not report["supported"] and not report["ok"]
    assert {check["kind"] for check in report["checks"]} == {"account", "workbook"}
    assert all(not check["supported"] for check in report["checks"])
