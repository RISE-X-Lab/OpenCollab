"""Structured, bounded verification and evidence-based repair decisions."""

from __future__ import annotations

import hashlib
import json


def select_contract(contract, scope="all", requirement_ids=(), *, shared_requirement_ids=None):
    selected = json.loads(json.dumps(contract))
    selected["scope"] = scope
    if scope != "all":
        selected.update(
            auth_checks=False,
            public_navigation=[],
            home_targets=[],
            sheet_create=False,
            sheet_contracts={},
            missing_inputs=[],
        )
        for key in ("public_repository", "account_menu", "compare_entry"):
            selected.pop(key, None)
        scenarios = contract.get("evolution_checks", [])
        if scope == "requirements":
            scenarios = [item for item in scenarios if set(item["source_ids"]).intersection(requirement_ids)]
        elif scope == "shared":
            # Shared navigation is read-only and independent of feature mutation scenarios.
            scenarios = []
        else:
            raise ValueError("Unknown acceptance scope")
        selected["evolution_checks"] = scenarios
    if scope == "shared" and shared_requirement_ids is not None:
        ready = set(shared_requirement_ids)
        inputs = selected.get("shared_inputs", {})
        inputs["sign_in_enabled"] = bool(ready.intersection(inputs.get("sign_in_ids", [])))
        for field, source in (("menu_account", "menu_ids"), ("repository", "repository_ids")):
            if not ready.intersection(inputs.get(source, [])):
                inputs.pop(field, None)
        if selected.get("kind") == "sheet":
            import re

            inputs.pop("workbook", None)
            for item in contract.get("evolution_checks", []):
                match = re.search(r"workbook `([^`]+)`", item.get("given", ""))
                if ready.intersection(item["source_ids"]) and match:
                    inputs.update(workbook=match.group(1), workbook_ids=item["source_ids"])
                    break
        selected["shared_inputs"] = inputs
        selected["shared_requirement_ids"] = sorted(ready)
    required = contract.get("required_scenarios", [])
    missing = contract.get("requirements_without_scenarios", [])
    if scope == "requirements":
        known = set(contract.get("required_requirement_ids", []))
        if not requirement_ids or set(requirement_ids) - known:
            raise ValueError("Supply original in-scope requirement IDs")
        required = [item for item in required if item["requirement_id"] in requirement_ids]
        missing = [key for key in missing if key in requirement_ids]
        selected["required_requirement_ids"] = [
            key for key in contract["required_requirement_ids"] if key in requirement_ids
        ]
    elif scope == "shared":
        required, missing = [], []
        selected["required_requirement_ids"] = []
    selected["requires_persistence"] = bool(
        set(selected["required_requirement_ids"]).intersection(contract.get("persistence_requirement_ids", []))
    )
    selected["required_scenarios"] = required
    selected["requirements_without_scenarios"] = missing
    selected["expected_scenarios"] = len(required)
    selected["mapped_scenarios"] = len(selected.get("evolution_checks", []))
    return selected


def source_digest(workspace):
    """Include app code/config and coordinator inputs, excluding reports and installed files."""
    digest = hashlib.sha256()
    # Acceptance semantics are part of the proof, including observation provenance.
    from pathlib import Path

    for path in sorted(Path(__file__).parent.glob("*")):
        if path.suffix in {".py", ".cjs", ".json"}:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    paths = []
    for name in ("frontend", "backend"):
        base = workspace / name
        if base.is_dir():
            paths.extend(p for p in base.glob("*") if p.is_file() and p.suffix in {".json", ".js", ".ts", ".html"})
            paths.extend(p for p in (base / "src").rglob("*") if p.is_file())
    paths.extend(workspace / ".arc/checks" / name for name in ("public-prerequisites.json", "evolution-baseline.json"))
    # Preservation gates inspect the delivered DB and tests before replaying a clean copy.
    # A cached source-only success must not mask later data/test corruption.
    from .delivery import database_files
    from .integrity import protected_test_files

    paths.extend(path for path in database_files(workspace / "backend") if not path.name.endswith(("-shm", "-journal")))
    digest.update(json.dumps(protected_test_files(workspace), sort_keys=True).encode())
    for path in sorted(set(paths)):
        if path.is_file():
            digest.update(path.relative_to(workspace).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def failure_cards(report):
    """Keep individual errors intact instead of truncating a JSON array of all failures."""
    cards = []
    for check in report.get("checks", []):
        if (
            check.get("ok")
            or check["step"] in {"browser_smoke", "scenario_coverage"}
            or (check["step"] == "stability_confirmation" and report.get("stability", {}).get("failures"))
        ):
            continue
        cards.append(
            {
                "name": check["step"],
                "kind": "infrastructure" if check.get("repairable") is False else "delivery",
                "detail": check.get("detail", "")[-1800:],
                "source_ids": [],
                "repairable": check.get("repairable") is not False,
            }
        )
    for check in report.get("browser", {}).get("prerequisites", []):
        if check.get("status") not in {"failed", "blocked", "skipped"}:
            continue
        cards.append(
            {
                "name": check["name"],
                "kind": check["status"],
                "scenario_id": check.get("scenario_id"),
                "requirement_id": check.get("requirement_id"),
                "scenario_index": check.get("scenario_index"),
                "blocked_by": check.get("blocked_by"),
                "source_ids": check.get("source_ids", []),
                "detail": check.get("detail", check.get("reason", ""))[:1800],
                "repairable": check["status"] != "skipped",
            }
        )
    # Surface crashes / empty reports even when no scenario result was written.
    browser = report.get("browser", {})
    errors = {key: browser[key] for key in ("error", "page_errors", "server_errors") if browser.get(key)}
    if errors:
        ids = sorted({key for error in browser.get("server_errors", []) for key in error.get("source_ids", [])})
        cards.append(
            {
                "name": "browser_runtime",
                "kind": "delivery",
                "source_ids": ids,
                "detail": json.dumps(errors, ensure_ascii=False)[:1800],
                "repairable": True,
            }
        )
    cards.extend(report.get("stability", {}).get("failures", []))
    if not cards and not report.get("ok"):
        cards = [
            {
                "name": c["step"],
                "kind": "delivery",
                "detail": c.get("detail", "")[:1800],
                "source_ids": [],
                "repairable": c.get("repairable") is not False,
            }
            for c in report.get("checks", [])
            if not c.get("ok")
        ]
    return cards


def repair_frontier(report):
    cards = [c for c in failure_cards(report) if c["repairable"]]
    # Build/startup/data integrity failures must be fixed before feature assertions.
    # An HTTP error in a running app does not block unrelated, already-reached assertions.
    early = [
        c for c in cards if c["kind"] == "delivery" and c["name"] not in {"written_state_restart", "browser_runtime"}
    ]
    shared = [c for c in cards if c["name"].startswith("shared:") or c.get("blocked_by")]
    targets = early or shared or cards
    # Deduplicate root blockers, retaining affected requirement IDs and examples.
    groups = {}
    for card in targets:
        key = card.get("blocked_by") or card.get("scenario_id") or card["name"]
        if key not in groups:
            groups[key] = dict(card, affected_checks=[])
        group = groups[key]
        group["affected_checks"].append(card["name"])
        group["source_ids"] = sorted(set(group["source_ids"]) | set(card["source_ids"]))
    return list(groups.values())


def summarize(report):
    return {
        "ok": report.get("ok", False),
        "scope": report.get("scope", "all"),
        "coverage": {key: value for key, value in report.get("scenario_coverage", {}).items() if key != "scenarios"},
        "failures": failure_cards(report),
        "repair_frontier": repair_frontier(report),
        "stability": report.get("stability"),
        "evidence_kind": "coordinator_checks_not_official_score",
    }


def improvement(before, after):
    def facts(report):
        passed = {"check:" + c["step"] for c in report.get("checks", []) if c.get("ok")}
        rows = report.get("browser", {}).get("prerequisites", [])
        passed.update(c.get("scenario_id") or c["name"] for c in rows if c.get("status") == "passed")
        blocked = {
            (c.get("scenario_id") or c["name"], c.get("blocked_by")) for c in rows if c.get("status") == "blocked"
        }
        return passed, blocked

    old_pass, old_blocked = facts(before)
    new_pass, new_blocked = facts(after)
    reached = {
        c.get("scenario_id") or c["name"]
        for c in after.get("browser", {}).get("prerequisites", [])
        if c.get("status") in {"passed", "failed"}
    }
    runtime_keys = ("error", "page_errors", "server_errors")
    old_browser, new_browser = before.get("browser", {}), after.get("browser", {})
    old_executed = {
        c.get("scenario_id") or c["name"]
        for c in old_browser.get("prerequisites", [])
        if c.get("status") in {"passed", "failed", "blocked"}
    }
    new_executed = {
        c.get("scenario_id") or c["name"]
        for c in new_browser.get("prerequisites", [])
        if c.get("status") in {"passed", "failed", "blocked"}
    }
    # Count a real runtime recovery, but never a missing, skipped, or narrower replay.
    runtime_recovered = (
        any(old_browser.get(key) for key in runtime_keys)
        and bool(new_browser)
        and not any(new_browser.get(key) for key in runtime_keys)
        and bool(old_executed)
        and old_executed <= new_executed
        and before.get("scope", "all") == after.get("scope", "all")
        and set(before.get("requirement_ids", [])) == set(after.get("requirement_ids", []))
    )
    # A root blocker becoming an actual feature failure is useful progress, even if fail count grows.
    return bool(
        after.get("ok")
        or new_pass - old_pass
        or runtime_recovered
        or any(name in reached for name, _ in old_blocked - new_blocked)
    )
