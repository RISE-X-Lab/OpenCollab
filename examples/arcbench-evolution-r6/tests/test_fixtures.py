"""Unknown schema capabilities remain explicit and unverified."""

import sqlite3

from arc_light.fixtures import verify_fixtures


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
