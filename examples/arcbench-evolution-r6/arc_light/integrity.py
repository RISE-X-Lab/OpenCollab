"""Read-only preservation evidence; never repairs the working database or tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path


def database_files(backend: Path) -> list[Path]:
    found = []
    suffixes = (".db", ".sqlite", ".sqlite3")
    for directory, dirs, files in os.walk(backend):
        depth = len(Path(directory).relative_to(backend).parts)
        dirs[:] = [name for name in dirs if name not in {"node_modules", ".git"} and depth < 3]
        for name in files:
            if name.endswith(suffixes) or any(
                name.endswith(suffix + sidecar) for suffix in suffixes for sidecar in ("-wal", "-shm", "-journal")
            ):
                found.append(Path(directory) / name)
    return sorted(found)


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def connect_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def consistent_backup(source, destination, timeout=20):
    destination = Path(destination)
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("Inherited SQLite snapshot exceeded deadline")

    try:
        with closing(connect_readonly(source)) as original, closing(sqlite3.connect(temporary)) as copy:
            original.backup(copy, pages=256, progress=progress, sleep=0.05)
            copy.execute("PRAGMA journal_mode=DELETE")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def row_hash(row):
    # Preserve SQLite value types and BLOB contents without logging business data.
    values = [(type(value).__name__, value.hex() if isinstance(value, bytes) else value) for value in row]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def database_values(path, expected=None):
    with closing(connect_readonly(path)) as connection:
        if expected is None and connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Inherited SQLite snapshot has foreign key violations")
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        result = {}
        for table in expected if expected is not None else tables:
            if table not in tables:
                continue
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")]
            selected = expected[table]["columns"] if expected is not None else columns
            if not set(selected) <= set(columns):
                result[table] = {"missing_columns": sorted(set(selected) - set(columns))}
                continue
            rows = connection.execute(f"SELECT {', '.join(map(quote, selected))} FROM {quote(table)}")
            counts = Counter(row_hash(row) for row in rows)
            result[table] = {"columns": selected, "rows": sum(counts.values()), "row_hashes": dict(counts)}
        return result


def inherited_damage(backend, baseline):
    damage = []
    for filename, expected in baseline.get("inherited_values", {}).items():
        path = (backend / filename).resolve()
        if not path.is_relative_to(backend.resolve()):
            raise ValueError("Invalid inherited database path")
        actual = database_values(path, expected) if path.is_file() else {}
        for table, original in expected.items():
            current = actual.get(table)
            missing = sum((Counter(original["row_hashes"]) - Counter((current or {}).get("row_hashes", {}))).values())
            if current is None or current.get("missing_columns") or missing:
                damage.append(
                    {
                        "database": filename,
                        "table": table,
                        "missing_table": current is None,
                        "missing_columns": (current or {}).get("missing_columns", []),
                        "removed_or_changed_rows": missing,
                    }
                )
    return damage


def protected_test_files(workspace):
    result = {}
    excluded = {"node_modules", ".git", ".arc", ".venv", "__pycache__", "dist", "build"}
    for directory, dirs, files in os.walk(workspace):
        dirs[:] = sorted(name for name in dirs if name not in excluded)
        parent = Path(directory)
        parts = {part.lower() for part in parent.relative_to(workspace).parts}
        for name in sorted(files):
            lower = name.lower()
            if (
                parts.intersection({"tests", "test", "__tests__", "e2e"})
                or ".test." in lower
                or ".spec." in lower
                or lower.startswith(("test_", "test-harness", "test_harness"))
                or lower.startswith(("vitest.config.", "jest.config.", "playwright.config."))
                or lower in {"pytest.ini", "conftest.py"}
            ):
                path = parent / name
                if path.is_file():
                    result[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def working_integrity(workspace):
    path = workspace / ".arc/checks/evolution-baseline.json"
    if not path.is_file():
        return [
            {
                "step": "baseline_capture",
                "ok": False,
                "repairable": False,
                "detail": "Original input snapshot is missing; preservation is unverified.",
            }
        ]
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("error"):
        return [
            {
                "step": "baseline_capture",
                "ok": False,
                "repairable": False,
                "detail": "Original baseline capture failed; preservation cannot be verified.",
            }
        ]
    checks = []
    if "protected_tests" in baseline:
        current = protected_test_files(workspace)
        changed = [name for name, digest in baseline["protected_tests"].items() if current.get(name) != digest]
        checks.append(
            {
                "step": "inherited_test_integrity",
                "ok": not changed,
                "repairable": True,
                "detail": json.dumps({"modified_or_removed": changed}),
            }
        )
    if "inherited_values" in baseline:
        try:
            damage = inherited_damage(workspace / "backend", baseline)
            checks.append(
                {
                    "step": "working_inherited_values",
                    "ok": not damage,
                    "repairable": True,
                    "detail": json.dumps(damage),
                    "values_verified": True,
                }
            )
        except (OSError, sqlite3.Error) as exc:
            checks.append(
                {
                    "step": "working_inherited_values",
                    "ok": False,
                    "repairable": False,
                    "detail": f"Cannot read working database: {type(exc).__name__}",
                }
            )
    return checks
