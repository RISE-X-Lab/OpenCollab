"""One final delivery check; no probe generation, browser install, or model tool."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

from arc_light.completion import scenario_coverage, scenario_key
from arc_light.fixtures import verify_fixtures
from arc_light.integrity import (
    consistent_backup,
    database_files,
    database_values,
    inherited_damage,
    protected_test_files,
    working_integrity,
)


def stop_process(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # Also kill descendants that outlived their direct parent.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("Verification cancelled; no acceptance verdict")


def run_command(
    args: list[str], cwd: Path, seconds: float, env: dict | None = None, cancel_event=None
) -> tuple[int, str]:
    check_cancelled(cancel_event)
    if seconds <= 0:
        return 124, "Verification deadline exhausted before this command"
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    )
    started = time.monotonic()
    last_notice = started
    try:
        while True:
            check_cancelled(cancel_event)
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                stop_process(proc)
                stdout, _ = proc.communicate(timeout=5)
                return 124, stdout + "\nCommand exceeded its verification deadline"
            try:
                stdout, _ = proc.communicate(timeout=min(2 if cancel_event is not None else 30, remaining))
                check_cancelled(cancel_event)
                return proc.returncode, stdout
            except subprocess.TimeoutExpired:
                if time.monotonic() - last_notice >= 29:
                    diagnostic(
                        "command_wait",
                        command=Path(args[0]).name,
                        cwd=cwd.name,
                        elapsed_seconds=round(time.monotonic() - started),
                    )
                    last_notice = time.monotonic()
    except BaseException:
        stop_process(proc)
        raise


def sqlite_snapshot(backend: Path, *, row_fingerprints=False) -> dict:
    snapshot = {}
    for path in database_files(backend):
        if not path.name.endswith((".db", ".sqlite", ".sqlite3")):
            continue
        with closing(sqlite3.connect(path)) as connection:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError(f"{path.name}: foreign key violations: {len(violations)}")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            data = {}
            for (table,) in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
                encoded = sorted(json.dumps(row, default=str, ensure_ascii=False) for row in rows)
                data[table] = {
                    "rows": len(rows),
                    "content_sha256": hashlib.sha256("\n".join(encoded).encode()).hexdigest(),
                }
                if row_fingerprints:
                    data[table]["row_fingerprints"] = [hashlib.sha256(row.encode()).hexdigest() for row in encoded]
            snapshot[path.relative_to(backend).as_posix()] = data
    return snapshot


def database_delta(before, after):
    changes = []
    for filename in sorted(before.keys() | after.keys()):
        old_tables, new_tables = before.get(filename, {}), after.get(filename, {})
        for table in sorted(old_tables.keys() | new_tables.keys()):
            old, new = old_tables.get(table, {}), new_tables.get(table, {})
            if old.get("content_sha256") == new.get("content_sha256"):
                continue
            removed = sum(
                (Counter(old.get("row_fingerprints", [])) - Counter(new.get("row_fingerprints", []))).values()
            )
            added = sum((Counter(new.get("row_fingerprints", [])) - Counter(old.get("row_fingerprints", []))).values())
            changes.append(
                {
                    "database": filename,
                    "table": table,
                    "before_rows": old.get("rows", 0),
                    "after_rows": new.get("rows", 0),
                    "added_rows": added,
                    "removed_or_changed_rows": removed,
                    "kind": "added_only" if not removed and table in old_tables else "changed_or_removed",
                }
            )
    return {
        "changes": changes,
        "unchanged": not changes,
        "guidance": "New related records after restart indicate delayed initialization; "
        "create them in the original transaction. "
        "Changed/removed records need migration/seed investigation. "
        "Never restore or delete user data to silence this check.",
    }


def _digest(package: Path) -> str:
    hasher = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        path = package / name
        hasher.update(path.read_bytes() if path.exists() else b"")
    return hasher.hexdigest()


def capture_baseline(workspace: Path, *, run_id=None, input_source=None) -> dict:
    """Consistent pre-edit DB contents and inherited test hashes, before any model work."""
    path = workspace / ".arc/checks/evolution-baseline.json"
    baseline_dir = path.parent / "inherited-databases"
    if path.exists() or baseline_dir.exists():
        raise FileExistsError("Original snapshot already exists; resume with load_baseline")
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline_dir.mkdir()
    names = []
    values = {}
    data = {
        "schema_version": 2,
        "protected_tests": protected_test_files(workspace),
        "run_id": run_id,
        "input_source": str(input_source) if input_source is not None else None,
    }
    try:
        # The SQLite backup API folds committed WAL contents into a coherent main DB.
        sources = [
            source
            for source in database_files(workspace / "backend")
            if source.name.endswith((".db", ".sqlite", ".sqlite3"))
        ]
        for number, source in enumerate(sources):
            consistent_backup(source, baseline_dir / str(number))
            name = source.relative_to(workspace / "backend").as_posix()
            names.append(name)
            values[name] = database_values(baseline_dir / str(number))
        # Derive row counts from the SAME committed snapshot, not a second live read.
        snapshot = {
            filename: {table: {"rows": item["rows"]} for table, item in tables.items()}
            for filename, tables in values.items()
        }
        data.update(databases=snapshot, inherited_values=values, error=None)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
        data.update(databases={}, inherited_values={}, error=f"{type(exc).__name__}: baseline capture failed")
    (baseline_dir / "manifest.json").write_text(json.dumps(names), encoding="utf-8")
    from .reports import write_json

    write_json(path, data)
    return data


def load_baseline(workspace: Path, *, run_id=None) -> dict:
    """Read the first input snapshot and verify its existing protection evidence."""
    path = workspace / ".arc/checks/evolution-baseline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 2
        or data.get("error")
        or not all(isinstance(data.get(key), dict) for key in ("protected_tests", "inherited_values", "databases"))
    ):
        raise ValueError("Original snapshot is incomplete or capture failed")
    if run_id is not None and data.get("run_id") != run_id:
        raise ValueError("Original snapshot belongs to a different run")
    directory = path.parent / "inherited-databases"
    names = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(names, list)
        or any(not isinstance(name, str) for name in names)
        or set(names) != set(data["inherited_values"])
        or len(names) != len(set(names))
    ):
        raise ValueError("Original snapshot manifest does not match recorded databases")
    for index, name in enumerate(names):
        if not (workspace / "backend" / name).resolve().is_relative_to((workspace / "backend").resolve()):
            raise ValueError("Invalid original database snapshot path")
        if database_values(directory / str(index)) != data["inherited_values"][name]:
            raise ValueError("Original database snapshot changed or is damaged")
    return data


def restore_inherited(workspace: Path, isolated: Path) -> bool:
    """Apply the pre-agent snapshot only to a disposable application copy."""
    if workspace.resolve() == isolated.resolve() or not (isolated / ".arc-isolated").is_file():
        raise ValueError("Inherited snapshots can only be applied to an isolated application copy")
    load_baseline(workspace)
    source = workspace / ".arc/checks/inherited-databases"
    manifest = source / "manifest.json"
    if not manifest.exists():
        return False
    backend = (isolated / "backend").resolve()
    targets = [(backend / name).resolve() for name in json.loads(manifest.read_text(encoding="utf-8"))]
    if any(not target.is_relative_to(backend) for target in targets):
        raise ValueError("Invalid inherited database snapshot path")
    for number in range(len(targets)):
        if not (source / str(number)).is_file():
            raise ValueError("Incomplete inherited database snapshot")
    for path in database_files(backend):
        path.unlink()
    for number, target in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / str(number), target)
    return True


def diagnostic(stage, **data):
    print(json.dumps({"diag": "delivery_progress", "stage": stage, **data}, ensure_ascii=False), flush=True)


@contextmanager
def isolated_application(workspace: Path, *, inherited=True):
    """Copy application files, sharing only installed modules. Never run its DB code in the source tree."""
    with tempfile.TemporaryDirectory(prefix="arc-isolated-") as directory:
        target = Path(directory)
        (target / ".arc-isolated").write_text(
            json.dumps({"kind": "disposable_application_copy", "inherited_snapshot_used": False}), encoding="utf-8"
        )
        for name in ("backend", "frontend"):
            source = workspace / name
            if not source.is_dir():
                continue
            shutil.copytree(
                source,
                target / name,
                ignore=shutil.ignore_patterns(
                    "node_modules", ".git", ".arc", "__pycache__", "test-results", "playwright-report"
                ),
            )
            modules = source / "node_modules"
            if modules.is_dir():
                link = target / name / "node_modules"
                if os.name == "nt":
                    result = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(link), str(modules.resolve())],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode:
                        raise OSError("Unable to share installed modules with isolated application")
                else:
                    link.symlink_to(modules.resolve(), target_is_directory=True)
        if inherited:
            restore_inherited(workspace, target)
            (target / ".arc-isolated").write_text(
                json.dumps({"kind": "disposable_application_copy", "inherited_snapshot_used": True}), encoding="utf-8"
            )
        yield target


def prepare_dependencies(workspace: Path, deadline: float, cancel_event=None) -> list[dict]:
    check_cancelled(cancel_event)
    output = workspace / ".arc/checks"
    output.mkdir(parents=True, exist_ok=True)
    cache_path = output / "dependency-cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    checks = []
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        return [{"step": "npm_available", "ok": False, "detail": "npm executable unavailable", "repairable": False}]
    for name in ("backend", "frontend"):
        check_cancelled(cancel_event)
        package = workspace / name
        if not (package / "package.json").is_file():
            checks.append(
                {"step": name + "_install", "ok": False, "detail": "package.json missing", "repairable": True}
            )
            continue
        digest = _digest(package)
        if cache.get(name) == digest and (package / "node_modules").is_dir():
            checks.append(
                {"step": name + "_install", "ok": True, "detail": "unchanged dependency manifest; install cached"}
            )
            continue
        diagnostic(name + "_install_start")
        code, text = run_command(
            [npm, "install", "--no-audit", "--no-fund", "--include=dev"],
            package,
            max(0, min(600, deadline - time.monotonic())),
            cancel_event=cancel_event,
        )
        network_error = network_failure(text)
        if code and network_error and deadline - time.monotonic() > 15:
            code, retry = run_command(
                [npm, "install", "--no-audit", "--no-fund", "--include=dev"],
                package,
                max(0, min(60, deadline - time.monotonic())),
                cancel_event=cancel_event,
            )
            text = text[-1500:] + "\nOne bounded network retry:\n" + retry
            network_error = network_failure(retry)
        checks.append(
            {
                "step": name + "_install",
                "ok": code == 0,
                "detail": text[-5000:],
                "repairable": code != 124 and not network_error,
            }
        )
        diagnostic(name + "_install_end", ok=code == 0, code=code)
        if code:
            break
        check_cancelled(cancel_event)
        cache[name] = _digest(package)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return checks


def probe_backend(backend: Path, log_path: Path, deadline: float, cancel_event=None) -> dict:
    check_cancelled(cancel_event)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    port = _port()
    env = {k: v for k, v in os.environ.items() if k not in {"ARC_DB_FILE", "ARC_E2E_DB_PATH", "DATABASE_FILE"}}
    env["PORT"] = str(port)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [npm, "run", "start"],
            cwd=backend,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    ready, status = False, "health endpoint not ready"
    try:
        while time.monotonic() < deadline and process.poll() is None:
            check_cancelled(cancel_event)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=max(0.1, min(2, deadline - time.monotonic()))
                ) as response:
                    if response.status == 200:
                        ready, status = True, "HTTP 200"
                        break
            except (OSError, urllib.error.URLError) as exc:
                status = str(exc)
            if cancel_event is None:
                time.sleep(0.2)
            else:
                cancel_event.wait(0.2)
    finally:
        stop_process(process)
    check_cancelled(cancel_event)
    return {
        "step": "backend_health",
        "ok": ready,
        "detail": status + "\n" + log_path.read_text(encoding="utf-8", errors="replace")[-4500:],
        "repairable": True,
    }


def preflight(workspace: Path, *, timeout=600, cancel_event=None) -> dict:
    """Early executable checks; failed app startup is evidence for the main agent, not acceptance."""
    output = workspace / ".arc/checks"
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    report = {"ok": False, "fatal": False, "evidence_kind": "environment_preflight", "checks": []}
    diagnostic("preflight_start")
    try:
        check_cancelled(cancel_event)
        node = shutil.which("node")
        if not node:
            report["checks"].append(
                {"step": "node_available", "ok": False, "detail": "node executable unavailable", "repairable": False}
            )
            report["fatal"] = True
            return report
        report["checks"].extend(prepare_dependencies(workspace, deadline, cancel_event=cancel_event))
        check_cancelled(cancel_event)
        failed = [c for c in report["checks"] if not c["ok"]]
        if failed:
            report["fatal"] = any(c.get("repairable") is False for c in failed)
            return report
        backend = workspace / "backend"
        package = json.loads((backend / "package.json").read_text(encoding="utf-8"))
        if "sqlite3" in {**package.get("dependencies", {}), **package.get("devDependencies", {})}:
            code, text = run_command(
                [
                    node,
                    "-e",
                    "const S=require('sqlite3'); "
                    "const d=new S.Database(':memory:',e=>{"
                    "if(e){console.error(e);process.exitCode=1;}else d.close();});",
                ],
                backend,
                max(0, min(20, deadline - time.monotonic())),
                cancel_event=cancel_event,
            )
            report["checks"].append(
                {"step": "sqlite3_module_load", "ok": code == 0, "detail": text[-3000:], "repairable": True}
            )
        code, text = run_command(
            [
                node,
                "-e",
                "const {chromium}=require('@playwright/test'); "
                "(async()=>{const b=await chromium.launch({headless:true,"
                "executablePath:process.env.ARC_BROWSER_EXECUTABLE||undefined});await b.close();})()"
                ".catch(e=>{console.error(e.message);process.exitCode=1;});",
            ],
            backend,
            max(0, min(30, deadline - time.monotonic())),
            cancel_event=cancel_event,
        )
        report["checks"].append(
            {"step": "browser_launch", "ok": code == 0, "detail": text[-3000:], "repairable": False}
        )
        check_cancelled(cancel_event)
        if deadline - time.monotonic() > 5:
            with isolated_application(workspace) as isolated:
                report["checks"].append(
                    probe_backend(
                        isolated / "backend",
                        output / "preflight-backend.log",
                        min(deadline, time.monotonic() + 45),
                        cancel_event=cancel_event,
                    )
                )
        else:
            report["checks"].append(
                {"step": "backend_health", "ok": False, "detail": "preflight deadline exhausted", "repairable": False}
            )
        check_cancelled(cancel_event)
        report["ok"] = all(c["ok"] for c in report["checks"])
        return report
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            report["cancelled"] = True
        report["checks"].append(
            {"step": "preflight_error", "ok": False, "detail": f"{type(exc).__name__}: {exc}", "repairable": False}
        )
        report["fatal"] = True
        return report
    finally:
        (output / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostic(
            "preflight_end",
            ok=report["ok"],
            fatal=report["fatal"],
            failed=[c["step"] for c in report["checks"] if not c["ok"]],
        )


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def network_failure(text: str) -> bool:
    return any(code in text.upper() for code in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT"))


def _verify_copy(workspace: Path, output: Path, *, timeout: float = 600, cancel_event=None) -> dict:
    workspace = workspace.resolve()
    backend, frontend = workspace / "backend", workspace / "frontend"
    output.mkdir(parents=True, exist_ok=True)
    # Reset once per verification, not on each restart: retain the failing request's logs.
    (output / "backend.log").write_text("", encoding="utf-8")
    deadline = time.monotonic() + timeout
    report = {
        "evidence_kind": "delivery_checks_not_official_score",
        "ok": False,
        "checks": [],
        "feature_coverage": "unverified",
        "official_score": None,
    }
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    node = shutil.which("node") or "node"
    process = None

    def record(step: str, ok: bool, detail: str = "", **extra) -> bool:
        report["checks"].append({"step": step, "ok": ok, "detail": detail[-5000:], **extra})
        diagnostic(step, ok=ok)
        return ok

    def command(args: list[str], cwd: Path, cap: float = 300, env: dict | None = None):
        return run_command(args, cwd, max(0, min(cap, deadline - time.monotonic())), env, cancel_event)

    def start(stage: str) -> tuple[subprocess.Popen, bool, str]:
        check_cancelled(cancel_event)
        if time.monotonic() >= deadline:
            raise TimeoutError("Verification deadline exhausted before backend startup")
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"ARC_DB_FILE", "ARC_E2E_DB_PATH", "DATABASE_FILE"}
        }
        env["PORT"] = str(port)
        log = open(output / "backend.log", "a", encoding="utf-8")
        log.write(f"\n[verification stage: {stage}]\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                [npm, "run", "start"],
                cwd=backend,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        finally:
            log.close()
        ready = False
        status = "no response"
        try:
            health_deadline = min(deadline, time.monotonic() + 45)
            while time.monotonic() < health_deadline and proc.poll() is None:
                check_cancelled(cancel_event)
                try:
                    with urllib.request.urlopen(
                        base_url + "/api/health", timeout=max(0.1, min(2, health_deadline - time.monotonic()))
                    ) as response:
                        status = f"HTTP {response.status}"
                        ready = response.status == 200
                        if ready:
                            break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    status = str(exc)
                time.sleep(0.2)
        except BaseException:
            stop_process(proc)
            raise
        return proc, ready, status

    try:
        if not backend.is_dir() or not frontend.is_dir():
            record("workspace", False, "Expected backend/ and frontend/")
            return report
        for path in sorted((backend / "src").rglob("*.js")):
            code, text = command([node, "--check", str(path)], backend, 30)
            if code:
                record("node_check", False, text, file=str(path.relative_to(workspace)), repairable=code != 124)
                return report
        record("node_check", True)
        code, text = command([npm, "run", "build"], frontend)
        if not record("frontend_build", code == 0, text, repairable=code != 124):
            return report
        report["database_isolation"] = "disposable_application_copy"
        marker = workspace / ".arc-isolated"
        report["inherited_snapshot_used"] = (
            json.loads(marker.read_text()).get("inherited_snapshot_used", False) if marker.is_file() else False
        )
        port = _port()
        base_url = f"http://127.0.0.1:{port}"
        process, ready, status = start("inherited_backend_start")
        if not record(
            "inherited_backend_start",
            ready,
            status + "\n" + (output / "backend.log").read_text(encoding="utf-8", errors="replace"),
        ):
            return report
        # Stop the first start before hashing: otherwise async DB writes can race the snapshot.
        stop_process(process)
        process = None
        first = sqlite_snapshot(backend)
        contract_path = output / "public-prerequisites.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.exists() else {}
        fixtures = verify_fixtures(backend, contract)
        (output / "fixtures.json").write_text(json.dumps(fixtures, indent=2), encoding="utf-8")
        record(
            "published_fixtures",
            fixtures["ok"],
            json.dumps([c for c in fixtures["checks"] if not c["ok"]]),
            repairable=all(check.get("supported", True) for check in fixtures["checks"]),
            supported=fixtures["supported"],
        )
        baseline_path = output / "evolution-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
        if baseline.get("error"):
            report["baseline_capability"] = baseline["error"]
        missing = []
        for filename, tables in baseline.get("databases", {}).items():
            for table, info in tables.items():
                if first.get(filename, {}).get(table, {}).get("rows", -1) < info["rows"]:
                    missing.append(filename + ":" + table)
        if not record(
            "inherited_row_counts",
            not missing,
            ", ".join(missing),
            values_verified=False,
            baseline_present=bool(baseline.get("databases")),
        ):
            return report
        if "inherited_values" in baseline:
            damage = inherited_damage(backend, baseline)
            if not record(
                "migrated_inherited_values", not damage, json.dumps(damage), values_verified=True, repairable=True
            ):
                return report
        process, ready, status = start("backend_restart")
        if not record("backend_restart", ready, status):
            return report
        stop_process(process)
        process = None
        second = sqlite_snapshot(backend)
        if not record(
            "seed_idempotent",
            ready and first == second,
            status,
            sqlite_found=bool(first),
            contents_unchanged=first == second,
            persistence_verified=False,
        ):
            return report
        process, ready, status = start("browser_backend_start")
        if not record("browser_backend_start", ready, status):
            return report
        module_root = Path(__file__).resolve().parent
        env = dict(os.environ, ARC_VERIFY_URL=base_url, ARC_VERIFY_OUTPUT=str(output))
        (output / "browser-report.json").unlink(missing_ok=True)
        if os.environ.get("ARC_BROWSER_SMOKE", "1") == "1":
            browser_seconds = max(1, min(480, deadline - time.monotonic() - 60))
            env["ARC_BROWSER_SECONDS"] = str(browser_seconds - 5)
            code, text = command([node, str(module_root / "browser_check.cjs")], backend, browser_seconds, env)
            browser_path = output / "browser-report.json"
            browser = json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else {}
            # Missing browser tooling is a capability limit, not an app repair task.
            unavailable = code == 78 or ("Cannot find module" in text and not browser)
            if unavailable:
                report["browser_capability"] = "unavailable; no install or model repair attempted"
                record("browser_smoke", False, report["browser_capability"], repairable=False)
            else:
                report["browser"] = browser
                errors = {
                    "page_errors": browser.get("page_errors", [])[:4],
                    "server_errors": browser.get("server_errors", [])[:4],
                    "bad_pages": [
                        {key: page.get(key) for key in ("url", "status", "blank", "navigation_error")}
                        for page in browser.get("pages", [])
                        if page.get("blank") or page.get("navigation_error") or (page.get("status") or 0) >= 400
                    ],
                    "error": browser.get("error"),
                }
                errors["failed_prerequisites"] = [
                    {
                        "name": check["name"],
                        "detail": "\n".join(check.get("detail", "").splitlines()[:8])[:350],
                        "source_ids": check.get("source_ids", []),
                        "status": check.get("status"),
                        "blocked_by": check.get("blocked_by"),
                    }
                    for check in browser.get("prerequisites", [])
                    if check.get("ok") is False
                ]
                report["skipped_prerequisites"] = [
                    {
                        "name": check["name"],
                        "reason": check.get("reason", ""),
                        "source_ids": check.get("source_ids", []),
                    }
                    for check in browser.get("prerequisites", [])
                    if check.get("status") == "skipped"
                ]
                errors["additional_failure_count"] = max(0, len(errors["failed_prerequisites"]) - 8)
                errors["failed_prerequisites"] = errors["failed_prerequisites"][:8]
                record(
                    "browser_smoke",
                    code == 0 and browser.get("ok") is True,
                    json.dumps(errors, ensure_ascii=False),
                    repairable=code != 124
                    and bool(
                        errors["failed_prerequisites"]
                        or errors["page_errors"]
                        or errors["server_errors"]
                        or errors["bad_pages"]
                        or errors["error"]
                    ),
                )
            coverage = scenario_coverage(
                contract.get("required_scenarios", []),
                browser.get("prerequisites", []),
                requirements_without_scenarios=contract.get("requirements_without_scenarios", []),
            )
            report["scenario_coverage"] = coverage
            (output / "scenario-ledger.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
            report["feature_coverage"] = "public_scenarios_passed" if coverage["complete"] else "incomplete"
            if contract.get("scope", "all") != "shared" and not coverage["complete"]:
                record(
                    "scenario_coverage",
                    False,
                    json.dumps([r for r in coverage["scenarios"] if r["status"] != "passed"]),
                    repairable=False,
                )
            # Browser contexts are closed: verify their writes survive an actual process restart.
            stop_process(process)
            process = None
            written = sqlite_snapshot(backend, row_fingerprints=True)
            process, ready, status = start("written_state_restart")
            stop_process(process)
            process = None
            restarted = sqlite_snapshot(backend, row_fingerprints=True)
            delta = database_delta(written, restarted)
            (output / "restart-diff.json").write_text(json.dumps(delta, indent=2), encoding="utf-8")
            record(
                "written_state_restart",
                ready and delta["unchanged"] and (not contract.get("requires_persistence") or bool(written)),
                status + "\n" + json.dumps(delta),
                repairable=True,
                persistence_verified=bool(written) and ready and delta["unchanged"],
            )
        else:
            report["browser_capability"] = "disabled"
            record("browser_smoke", False, "Browser verification disabled; acceptance unverified", repairable=False)
        if contract.get("scope") == "shared" and not browser.get("prerequisites"):
            record("shared_coverage", False, "No shared checks executed", repairable=False)
        report["ok"] = all(check["ok"] for check in report["checks"])
        return report
    except Exception as exc:
        record(
            "verification_error",
            False,
            f"{type(exc).__name__}: {exc}",
            repairable=not isinstance(exc, (OSError, TimeoutError)),
        )
        return report
    finally:
        if process is not None:
            stop_process(process)
        report["seconds_remaining"] = round(max(0, deadline - time.monotonic()), 1)
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def verify(
    workspace: Path,
    *,
    timeout: float = 600,
    scope="all",
    requirement_ids=(),
    output_dir=None,
    cancel_event=None,
    scenario_names=None,
    confirm_stability=False,
    shared_requirement_ids=None,
) -> dict:
    from .compatibility import enrich_contract
    from .evidence import select_contract, source_digest
    from .stability import certify, observe

    workspace = workspace.resolve()
    source_output = workspace / ".arc/checks"
    output = Path(output_dir) if output_dir is not None else source_output
    output.mkdir(parents=True, exist_ok=True)
    contract_file = source_output / "public-prerequisites.json"
    try:
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
        if not contract.get("required_requirement_ids") or "required_scenarios" not in contract:
            raise ValueError("Missing original task scope; generate checks from the public requirements")
        load_baseline(workspace)
    except (OSError, ValueError, sqlite3.Error) as exc:
        from .reports import write_json

        report = {
            "ok": False,
            "scope": scope,
            "requirement_ids": list(requirement_ids),
            "checks": [
                {
                    "step": "verification_inputs",
                    "ok": False,
                    "repairable": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ],
            "evidence_kind": "delivery_checks_not_official_score",
        }
        write_json(output / "verification.json", report)
        return report
    contract = enrich_contract(contract)
    selected = select_contract(contract, scope, requirement_ids, shared_requirement_ids=shared_requirement_ids)
    if scenario_names is not None:
        if scope != "requirements":
            raise ValueError("Exact scenario replays require requirements scope")
        selected["evolution_checks"] = [
            item for item in selected.get("evolution_checks", []) if scenario_key(item) in scenario_names
        ]
        if len(selected["evolution_checks"]) != len(set(scenario_names)):
            raise ValueError("Unknown or out-of-scope stability scenario")
        selected["required_scenarios"] = [
            item for item in selected["required_scenarios"] if scenario_key(item) in scenario_names
        ]
        selected["requirements_without_scenarios"] = []
        selected["expected_scenarios"] = len(selected["required_scenarios"])
    # The complete source contract is immutable; focused output lives in its own directory.
    if output != source_output:
        (output / "public-prerequisites.json").write_text(json.dumps(selected), encoding="utf-8")
        baseline = source_output / "evolution-baseline.json"
        if baseline.is_file():
            shutil.copy2(baseline, output / baseline.name)
    elif scope != "all":
        raise ValueError("Focused checks require an independent output directory")
    else:
        # Upgrade attributed coordinator metadata, never change the public cards.
        contract_file.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    # A build/install failure must not leave yesterday's browser success looking current.
    for name in ("browser-report.json", "browser-progress.json", "restart-diff.json", "stability-report.json"):
        (output / name).unlink(missing_ok=True)
    (output / "scenario-ledger.json").write_text(
        json.dumps(
            scenario_coverage(
                selected.get("required_scenarios", []),
                [],
                requirements_without_scenarios=selected.get("requirements_without_scenarios", []),
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    deadline = time.monotonic() + timeout
    digest = source_digest(workspace)
    report = {"ok": False, "checks": [], "evidence_kind": "delivery_checks_not_official_score"}
    diagnostic("verification_start")
    try:
        checks = working_integrity(workspace)
        report["checks"] = checks
        if any(not c["ok"] for c in checks):
            return report
        checks += prepare_dependencies(workspace, deadline, cancel_event)
        report["checks"] = checks
        if any(not c["ok"] for c in checks):
            return report
        with isolated_application(workspace) as isolated:
            check_cancelled(cancel_event)
            report = _verify_copy(
                isolated, output, timeout=max(0, deadline - time.monotonic()), cancel_event=cancel_event
            )
            report["checks"] = checks + report["checks"]
        if source_digest(workspace) != digest:
            report["checks"].append(
                {
                    "step": "source_changed_during_verification",
                    "ok": False,
                    "detail": "Source changed; this run cannot certify delivery",
                    "repairable": False,
                }
            )
            report["ok"] = False
        else:
            observe(workspace, report, digest, output / "verification.json")
            if confirm_stability and scope == "all" and report["ok"]:
                report = certify(
                    workspace,
                    report,
                    contract=contract,
                    digest=digest,
                    timeout=deadline - time.monotonic(),
                    output=output,
                    cancel_event=cancel_event,
                    verifier=verify,
                )
        return report
    except Exception as exc:
        report["ok"] = False
        report["checks"].append(
            {"step": "isolation_error", "ok": False, "detail": f"{type(exc).__name__}: {exc}", "repairable": False}
        )
        return report
    finally:
        report["scope"] = scope
        report["requirement_ids"] = list(requirement_ids)
        report["duration_seconds"] = round(max(0, timeout - (deadline - time.monotonic())), 2)
        report["seconds_remaining"] = round(max(0, deadline - time.monotonic()), 1)
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostic("verification_end", ok=report["ok"])
