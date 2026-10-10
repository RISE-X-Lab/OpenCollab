"""Run the actual Node adapter and Chromium against a hand-written SQLite app."""

import json
import os
import shutil
import sqlite3

import pytest
from arc_light.delivery import _digest, capture_baseline, database_values, preflight, verify
from arc_light.evidence import repair_frontier
from arc_light.public_checks import write_public_checks
from arc_light.reports import write_json

pytestmark = pytest.mark.skipif(os.environ.get("ARCBENCH_BROWSER_TESTS") != "1", reason="Separate browser fixture job")


def fixture_app(tmp_path, *, names=("alice", "bob"), unknown=False, empty=False):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("backend", "frontend"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "node_modules").symlink_to(root / "node_modules", target_is_directory=True)
        scripts = (
            {"start": "node src/server.cjs"}
            if name == "backend"
            else {"build": "node -e \"console.log('fixture build')\""}
        )
        (directory / "package.json").write_text(json.dumps({"name": "fixture-" + name, "scripts": scripts}))
    source = tmp_path / "backend/src"
    source.mkdir()
    shutil.copyfile(root / "tests/fixtures/server.cjs", source / "server.cjs")
    # Prepare the inherited schema through the real runtime, before its input snapshot.
    from arc_light.delivery import run_command

    code, text = run_command(
        [
            "node",
            "-e",
            "const{DatabaseSync}=require('node:sqlite');const db=new DatabaseSync('app.sqlite');"
            'db.exec("CREATE TABLE accounts(username TEXT PRIMARY KEY,email TEXT,password TEXT);'
            "INSERT INTO accounts VALUES('alice','alice@example.test','fixture-password');"
            "INSERT INTO accounts VALUES('bob','bob@example.test','fixture-password');\");db.close()",
        ],
        tmp_path / "backend",
        20,
    )
    assert code == 0, text
    document = [
        {
            "id": "REQ-" + username,
            "name": "Unknown action" if unknown else "Sign In with an Existing Account",
            "description": "Modified Feature Description. Sign in and reload.",
            "scenarios": []
            if empty
            else [
                {
                    "steps": [
                        {
                            "keyword": "GIVEN",
                            "content": f"account `{username}` (`{username}@example.test`, `fixture-password`)",
                        },
                        {"keyword": "WHEN", "content": f"enters `{username}` and `fixture-password`"},
                        {"keyword": "THEN", "content": "the signed-in username remains after reload"},
                    ]
                }
            ],
        }
        for username in names
    ]
    write_public_checks(document, tmp_path)
    capture_baseline(tmp_path)
    write_json(
        tmp_path / ".arc/checks/dependency-cache.json",
        {name: _digest(tmp_path / name) for name in ("frontend", "backend")},
    )
    return tmp_path


def test_real_browser_same_name_scenarios_and_sqlite_restart(tmp_path, monkeypatch):
    workspace = fixture_app(tmp_path)
    original = database_values(workspace / "backend/app.sqlite")
    from arc_light import stability

    monkeypatch.setattr(stability, "DEFAULT_PROBES", {("Sign In with an Existing Account", 0)})
    report = verify(workspace, timeout=120, confirm_stability=True)
    assert report["ok"], report
    assert all(row["consecutive_passes"] == 3 for row in report["stability"]["checks"])
    assert len(report["stability"]["runs"]) == 2
    assert report["scenario_coverage"]["expected"] == 2
    assert report["scenario_coverage"]["passed"] == 2
    rows = report["browser"]["prerequisites"]
    assert {row["requirement_id"] for row in rows if "requirement_id" in row} == {"REQ-alice", "REQ-bob"}
    assert next(c for c in report["checks"] if c["step"] == "seed_idempotent")["ok"]
    assert next(c for c in report["checks"] if c["step"] == "written_state_restart")["persistence_verified"]
    assert database_values(workspace / "backend/app.sqlite") == original


def test_real_500_preserves_independent_business_failure(tmp_path, monkeypatch):
    workspace = fixture_app(tmp_path)
    monkeypatch.setenv("ARC_FIXTURE_FAILURES", "1")
    report = verify(workspace, timeout=120)
    assert not report["ok"]
    rows = report["browser"]["prerequisites"]
    assert next(r for r in rows if r.get("requirement_id") == "REQ-alice")["status"] == "passed"
    assert next(r for r in rows if r.get("requirement_id") == "REQ-bob")["status"] == "failed"
    frontier = repair_frontier(report)
    assert any(card["name"] == "browser_runtime" for card in frontier)
    assert any("REQ-bob" in card["source_ids"] for card in frontier if card["name"] != "browser_runtime")


@pytest.mark.parametrize("unknown,empty", [(True, False), (False, True)])
def test_real_build_and_browser_cannot_pass_unmapped_or_zero_scenarios(tmp_path, unknown, empty):
    workspace = fixture_app(tmp_path, names=("alice",), unknown=unknown, empty=empty)
    report = verify(workspace, timeout=120)
    assert next(c for c in report["checks"] if c["step"] == "frontend_build")["ok"]
    assert not report["ok"] and not report["scenario_coverage"]["complete"]
    assert not next(c for c in report["checks"] if c["step"] == "scenario_coverage")["ok"]


def test_missing_browser_reports_capability_and_keeps_original_database(tmp_path, monkeypatch):
    workspace = fixture_app(tmp_path, names=("alice",))
    before = (workspace / "backend/app.sqlite").read_bytes()
    monkeypatch.setenv("ARC_BROWSER_EXECUTABLE", str(workspace / "missing-browser"))
    report = verify(workspace, timeout=120)
    assert not report["ok"] and "unavailable" in report["browser_capability"]
    assert not report["scenario_coverage"]["complete"]
    assert (workspace / "backend/app.sqlite").read_bytes() == before


def test_half_application_reports_missing_package_before_generation(tmp_path):
    workspace = fixture_app(tmp_path, names=("alice",))
    shutil.rmtree(workspace / "frontend")
    report = verify(workspace, timeout=120)
    assert not report["ok"]
    assert not next(check for check in report["checks"] if check["step"] == "frontend_install")["ok"]


def test_relative_output_directory_reaches_the_same_browser_contract(tmp_path, monkeypatch):
    from pathlib import Path

    workspace = fixture_app(tmp_path, names=("alice",))
    monkeypatch.chdir(workspace)
    report = verify(
        workspace,
        timeout=120,
        scope="requirements",
        requirement_ids=["REQ-alice"],
        output_dir=Path(".arc/checks/focused"),
    )
    assert report["ok"], report
    assert report["scenario_coverage"]["passed"] == 1
    output = workspace / ".arc/checks/focused"
    assert json.loads((output / "browser-report.json").read_text())["ok"]
    assert json.loads((output / "verification.json").read_text())["ok"]


@pytest.mark.parametrize("destructive", [False, True])
def test_install_lifecycle_runs_in_each_copy_and_keeps_original_data_and_source(tmp_path, destructive):
    workspace = fixture_app(tmp_path, names=("alice",))
    backend = workspace / "backend"
    with sqlite3.connect(backend / "app.sqlite") as db:
        db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO records VALUES(1,'inherited')")
    (workspace / ".arc/checks/evolution-baseline.json").unlink()
    shutil.rmtree(workspace / ".arc/checks/inherited-databases")
    capture_baseline(workspace)
    modules = backend / "node_modules"
    shared = modules.resolve()
    modules.unlink()
    modules.mkdir()
    (modules / "@playwright").mkdir()
    (modules / "@playwright/test").symlink_to(shared / "@playwright/test", target_is_directory=True)
    package_path = backend / "package.json"
    package = json.loads(package_path.read_text())
    package["scripts"]["postinstall"] = "node src/install.cjs"
    package_path.write_text(json.dumps(package))
    sql = "DELETE FROM records" if destructive else "CREATE TABLE installed(value TEXT)"
    (backend / "src/install.cjs").write_text(
        "const fs=require('node:fs');const{DatabaseSync}=require('node:sqlite');"
        "fs.writeFileSync('src/generated.cjs','module.exports=true');"
        "fs.mkdirSync('node_modules/@playwright',{recursive:true});"
        f"fs.symlinkSync({json.dumps(str(shared / '@playwright/test'))},'node_modules/@playwright/test','dir');"
        "fs.writeFileSync('node_modules/lifecycle-ran','copy');"
        f"const db=new DatabaseSync('app.sqlite');db.exec({json.dumps(sql)});db.close();"
    )
    server = backend / "src/server.cjs"
    server.write_text("require('./generated.cjs');\n" + server.read_text())
    before = {
        path.relative_to(workspace): path.read_bytes()
        for name in ("backend", "frontend")
        for path in (workspace / name).rglob("*")
        if path.is_file() and "node_modules" not in path.relative_to(workspace).parts
    }
    for _ in range(2):
        report = verify(workspace, timeout=120)
        if destructive:
            assert not report["ok"]
            assert not next(check for check in report["checks"] if check["step"] == "inherited_row_counts")["ok"]
        else:
            assert report["ok"], report
        after = {
            path.relative_to(workspace): path.read_bytes()
            for name in ("backend", "frontend")
            for path in (workspace / name).rglob("*")
            if path.is_file() and "node_modules" not in path.relative_to(workspace).parts
        }
        assert after == before
        assert not (modules / "lifecycle-ran").exists()


def test_preflight_installs_real_working_dependencies_and_retains_generated_source(tmp_path):
    from arc_light.delivery import run_command

    (tmp_path / "app").mkdir()
    workspace = fixture_app(tmp_path / "app", names=("alice",))
    shared = (workspace / "backend/node_modules").resolve()
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    (dependency / "package.json").write_text(json.dumps({"name": "fixture-local-module", "version": "1.0.0"}))
    (dependency / "index.js").write_text("module.exports='installed'")
    for name in ("backend", "frontend"):
        directory = workspace / name
        (directory / "node_modules").unlink()
        package_path = directory / "package.json"
        package = json.loads(package_path.read_text())
        package["dependencies"] = {"fixture-local-module": "file:" + str(dependency)}
        if name == "backend":
            package["scripts"]["postinstall"] = "node src/install.cjs"
            (directory / "src/install.cjs").write_text(
                "const fs=require('node:fs');"
                "fs.writeFileSync('src/generated.cjs','module.exports=true');"
                "fs.mkdirSync('node_modules/@playwright',{recursive:true});"
                f"fs.symlinkSync({json.dumps(str(shared / '@playwright/test'))},'node_modules/@playwright/test','dir');"
            )
            server = directory / "src/server.cjs"
            server.write_text("require('./generated.cjs');require('fixture-local-module');\n" + server.read_text())
        else:
            package["scripts"]["build"] = (
                "node -e \"if(require('fixture-local-module')!=='installed')throw Error('missing dependency')\""
            )
        package_path.write_text(json.dumps(package))
    for iteration in range(2):
        report = preflight(workspace, timeout=120)
        assert report["ok"], report
        for name in ("backend", "frontend"):
            assert (workspace / name / "node_modules/fixture-local-module").is_dir()
        assert (workspace / "backend/src/generated.cjs").is_file()
        if iteration:
            installs = [check for check in report["checks"] if check["step"].endswith("_install")]
            assert all("install cached" in check["detail"] for check in installs)
    code, text = run_command(["npm", "run", "build"], workspace / "frontend", 20)
    assert code == 0, text
    code, text = run_command(
        ["node", "-e", "require('./src/generated.cjs');require('fixture-local-module')"], workspace / "backend", 20
    )
    assert code == 0, text
    assert (workspace / "backend/node_modules/@playwright/test").is_dir()


@pytest.mark.parametrize("visitor", [False, True])
def test_reaction_feedback_count_and_real_reload(tmp_path, visitor):
    workspace = fixture_app(tmp_path, names=("alice",))
    given = (
        "visitor in an unauthenticated session with issue `Public issue`"
        if visitor
        else "account `alice` (`alice@example.test`, `fixture-password`) and issue `Public issue`"
    )
    node = {
        "id": "REQ-reaction",
        "name": "Add and Remove Issue Reactions",
        "scenarios": [
            {
                "steps": [
                    {"keyword": "GIVEN", "content": given},
                    {"keyword": "WHEN", "content": "add a reaction"},
                    {"keyword": "THEN", "content": "total remains after reload"},
                ]
            },
            {
                "steps": [
                    {"keyword": "GIVEN", "content": given},
                    {"keyword": "WHEN", "content": "add then remove a reaction"},
                    {"keyword": "THEN", "content": "original total remains after reload"},
                ]
            },
        ],
    }
    write_public_checks(node, workspace)
    index = 0 if visitor else 1
    report = verify(
        workspace,
        timeout=120,
        scope="requirements",
        requirement_ids=["REQ-reaction"],
        scenario_names=[f"REQ-reaction:scenario_{index + 1}"],
        output_dir=workspace / ".arc/checks/reaction",
    )
    assert report["ok"], report
    assert report["scenario_coverage"]["passed"] == 1
    assert report["inherited_snapshot_used"]


def test_reaction_scenarios_keep_independent_given_and_reuse_one_build(tmp_path, monkeypatch):
    workspace = fixture_app(tmp_path)
    counter = tmp_path / "build-count.txt"
    monkeypatch.setenv("ARC_FIXTURE_BUILD_COUNT", str(counter))
    monkeypatch.setenv("ARC_VERIFY_CONTRACT", str(tmp_path / "other-run-contract.json"))
    package_path = workspace / "frontend/package.json"
    package = json.loads(package_path.read_text())
    package["scripts"]["build"] = (
        "node -e \"require('node:fs').appendFileSync(process.env.ARC_FIXTURE_BUILD_COUNT,'build\\n')\""
    )
    package_path.write_text(json.dumps(package))
    write_json(
        workspace / ".arc/checks/dependency-cache.json",
        {name: _digest(workspace / name) for name in ("frontend", "backend")},
    )
    document = [
        {
            "id": "REQ-" + username,
            "name": "Sign In with an Existing Account",
            "scenarios": [
                {
                    "steps": [
                        {
                            "keyword": "GIVEN",
                            "content": f"account `{username}` (`{username}@example.test`, `fixture-password`)",
                        },
                        {"keyword": "WHEN", "content": f"enters `{username}` and `fixture-password`"},
                        {"keyword": "THEN", "content": "the signed-in username remains after reload"},
                    ]
                }
            ],
        }
        for username in ("alice", "bob")
    ]
    given = "account `alice` (`alice@example.test`, `fixture-password`) and issue `Public issue`"
    document.append(
        {
            "id": "REQ-reaction",
            "name": "Add and Remove Issue Reactions",
            "scenarios": [
                {
                    "steps": [
                        {"keyword": "GIVEN", "content": given},
                        {
                            "keyword": "WHEN",
                            "content": "add a reaction" if index == 0 else "add then remove a reaction",
                        },
                        {"keyword": "THEN", "content": "total remains after reload"},
                    ]
                }
                for index in range(2)
            ],
        }
    )
    write_public_checks(document, workspace)
    original = (workspace / "backend/app.sqlite").read_bytes()
    report = verify(workspace, timeout=120)
    assert report["ok"], report
    assert report["scenario_coverage"]["expected"] == report["scenario_coverage"]["passed"] == 4
    assert counter.read_text().splitlines() == ["build"]
    assert len(report["reaction_runs"]) == 2
    assert all(row["persistence_verified"] for row in report["reaction_runs"])
    assert (workspace / "backend/app.sqlite").read_bytes() == original
    from arc_light.stability import read_history

    assert all(row["consecutive_passes"] == 1 for row in read_history(workspace)["checks"].values())


def test_filter_duplicate_name_precedes_missing_filter_and_delete_survives_reload(tmp_path):
    workspace = fixture_app(tmp_path, names=("alice",))
    scenario = {
        "steps": [
            {"keyword": "GIVEN", "content": "workbook `Public workbook` with saved filter view `Active`"},
            {"keyword": "WHEN", "content": 'creates a filter for A1:B2 and enters ` Active ` in "Filter view name"'},
            {"keyword": "THEN", "content": "the duplicate view is rejected before a missing-filter precondition"},
        ]
    }
    document = [
        {"id": "REQ-filter", "name": "Filter Rows by Value or Condition", "scenarios": [scenario] * 3},
        {"id": "REQ-sheet-kind", "name": "Freeze Rows and Columns", "scenarios": []},
    ]
    write_public_checks(document, workspace, requirement_ids=["REQ-filter"])
    report = verify(
        workspace,
        timeout=120,
        scope="requirements",
        requirement_ids=["REQ-filter"],
        scenario_names=["REQ-filter:scenario_3"],
        output_dir=workspace / ".arc/checks/filter",
    )
    assert report["ok"], report
    assert report["scenario_coverage"]["scenarios"][0]["scenario_index"] == 2
    assert next(c for c in report["checks"] if c["step"] == "written_state_restart")["persistence_verified"]
