"""Read-only checks for explicitly published account/workbook GIVEN inputs.

Other schemas are reported as unsupported, never silently certified. UI acceptance
still verifies repository permissions, branches and feature metadata.
"""

import re
import sqlite3
from contextlib import closing

from .integrity import database_files


def verify_fixtures(backend, contract):
    checks = []
    files = [p for p in database_files(backend) if p.suffix in {".db", ".sqlite", ".sqlite3"}]
    accounts, workbooks = {}, set()
    has_accounts = has_workbooks = False
    for path in files:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "accounts" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
                if {"username", "email"} <= columns:
                    has_accounts = True
                    accounts.update(db.execute("SELECT username, email FROM accounts"))
            if "workbooks" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(workbooks)")}
                if "name" in columns:
                    has_workbooks = True
                    workbooks.update(row[0] for row in db.execute("SELECT name FROM workbooks"))
    for item in contract.get("evolution_checks", []):
        account = item.get("account")
        if account and not has_accounts:
            checks.append(
                {
                    "kind": "account",
                    "ok": False,
                    "supported": False,
                    "source_ids": item["source_ids"],
                    "detail": "Account schema is unsupported; GIVEN identity is unverified",
                }
            )
        if account and has_accounts:
            username = account["username"]
            ok = accounts.get(username, "").lower() == account["email"].lower()
            checks.append(
                {
                    "kind": "account",
                    "name": username,
                    "ok": ok,
                    "source_ids": item["source_ids"],
                    "scenario_index": item["scenario_index"],
                    "detail": ""
                    if ok
                    else "Required GIVEN account is absent or has a different email; "
                    "repair idempotent provisioning code",
                }
            )
        match = re.search(r"workbook\s+`([^`]+)`", item["given"], re.I)
        if match and not has_workbooks:
            checks.append(
                {
                    "kind": "workbook",
                    "ok": False,
                    "supported": False,
                    "source_ids": item["source_ids"],
                    "detail": "Workbook schema is unsupported; GIVEN input is unverified",
                }
            )
        if has_workbooks:
            match = re.search(r"workbook\s+`([^`]+)`", item["given"], re.I)
            if match:
                name = match.group(1)
                checks.append(
                    {
                        "kind": "workbook",
                        "name": name,
                        "ok": name in workbooks,
                        "source_ids": item["source_ids"],
                        "scenario_index": item["scenario_index"],
                        "detail": "" if name in workbooks else "Required GIVEN workbook is absent",
                    }
                )
    return {
        "scope": "published_account_identity_and_workbook_presence_only",
        "supported": has_accounts or has_workbooks,
        "checks": checks,
        "ok": all(check["ok"] for check in checks),
    }
