"""Persist real failures and require fresh, consecutive evidence for timing-sensitive paths."""

from __future__ import annotations

import json
import re

from .completion import scenario_key
from .reports import write_json

REQUIRED_PASSES = 3
DEFAULT_PROBES = {
    ("Archive and Restore a Repository", 0),
    ("Freeze Rows and Columns", 2),
}
PERSISTENT_FEATURES = {
    "Archive and Restore a Repository",
    "Freeze Rows and Columns",
    "Edit a Cell Through the Grid or Formula Bar",
    "Find and Replace Cell Text",
    "Create and Edit Named Ranges",
    "Create, Edit, and Delete a Cell Note",
    "Create, Edit, and Delete Conditional Formatting Rules",
}


def scenario_name(item):
    return scenario_key(item)


def read_history(workspace):
    path = workspace / ".arc/checks/stability-history.json"
    if not path.exists():
        return {"version": 1, "runs": 0, "checks": {}}
    # Malformed history is an evidence failure; never silently forget known failures.
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("checks"), dict):
        raise ValueError("Invalid stability history; cannot certify consecutive passes")
    normalized = {}
    for key, row in value["checks"].items():
        if key.startswith("evolution:"):
            # Preserve known legacy failures while upgrading their original domain identity.
            match = re.search(r":scenario_(\d+)$", key)
            ids = row.get("source_ids", [])
            if not match or len(ids) != 1:
                raise ValueError("Ambiguous legacy stability history; original requirement identity needed")
            key = scenario_key({"requirement_id": ids[0], "scenario_index": int(match.group(1)) - 1})
        if key in normalized:
            raise ValueError("Duplicate stability scenario identity")
        normalized[key] = row
    value["checks"] = normalized
    return value


def observe(workspace, report, digest, report_path):
    history = read_history(workspace)
    history["runs"] += 1
    rows = report.get("browser", {}).get("prerequisites", [])
    for row in rows:
        name = row["name"]
        if not name.startswith("evolution:") or not re.search(r":scenario_\d+$", name):
            continue
        key = row.get("scenario_id")
        if not key or "requirement_id" not in row or "scenario_index" not in row:
            raise ValueError("Browser scenario result lacks its original requirement identity")
        previous = history["checks"].get(key, {})
        matches = [item for item in rows if item.get("scenario_id") == key]
        status = row.get("status", "unverified") if len(matches) == 1 else "unverified"
        same = previous.get("source_digest") == digest
        streak = previous.get("consecutive_passes", 0) if same else 0
        current = dict(
            previous,
            status=status,
            source_digest=digest,
            source_ids=row.get("source_ids", []),
            run=history["runs"],
            report_path=str(report_path),
            consecutive_passes=min(REQUIRED_PASSES, streak + 1) if status == "passed" else 0,
        )
        if status == "failed":
            current["failure_count"] = previous.get("failure_count", 0) + 1
            current["last_failure"] = {
                "run": history["runs"],
                "source_digest": digest,
                "detail": row.get("detail", "")[:1800],
                "report_path": str(report_path),
            }
        history["checks"][key] = current
    write_json(workspace / ".arc/checks/stability-history.json", history)
    return history


def targets(contract, history):
    selected = []
    for item in contract.get("evolution_checks", []):
        name = scenario_name(item)
        prior = history["checks"].get(name, {})
        if (item["name"], item["scenario_index"]) in DEFAULT_PROBES or (
            item["name"] in PERSISTENT_FEATURES and prior.get("last_failure")
        ):
            selected.append(item)
    return selected


def pending(items, history, digest):
    return [
        item
        for item in items
        if history["checks"].get(scenario_name(item), {}).get("source_digest") != digest
        or history["checks"].get(scenario_name(item), {}).get("consecutive_passes", 0) < REQUIRED_PASSES
    ]


def cache_signature(workspace, scope, ids):
    """A newer replay failure invalidates cached success even with unchanged source."""
    rows = read_history(workspace)["checks"]
    return tuple(
        sorted(
            (
                name,
                row.get("status"),
                row.get("source_digest"),
                row.get("consecutive_passes", 0),
                row.get("failure_count", 0),
            )
            for name, row in rows.items()
            if scope == "all" or (scope == "requirements" and set(ids).intersection(row.get("source_ids", [])))
        )
    )


def certify(workspace, report, *, contract, digest, timeout, output, cancel_event, verifier):
    """Only final full acceptance calls this. Replays use new app/DB copies, never a cache.

    Failed probes stop immediately and reach the repair loop. Time/capability limits
    remain unverified. Existing full coverage is retained, not replaced by a subset.
    """
    import time

    from .delivery import check_cancelled
    from .evidence import failure_cards, source_digest

    deadline = time.monotonic() + max(0, timeout)
    history = read_history(workspace)
    items = targets(contract, history)
    if not items:
        return report
    runs, failures = [], []
    for _ in range(1, REQUIRED_PASSES):
        needed = pending(items, history, digest)
        if not needed:
            break
        check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining < 60:
            break
        selected_names = [scenario_name(item) for item in needed]
        ids = sorted({key for item in needed for key in item["source_ids"]})
        path = output / "stability" / str(history["runs"] + 1)
        # verify observes each real run. An independent output preserves the complete ledger.
        fresh = verifier(
            workspace,
            timeout=remaining,
            scope="requirements",
            requirement_ids=ids,
            scenario_names=selected_names,
            output_dir=path,
            cancel_event=cancel_event,
        )
        runs.append(
            {"report_path": str(path / "verification.json"), "ok": fresh["ok"], "scenario_names": selected_names}
        )
        history = read_history(workspace)
        if not fresh["ok"]:
            failures = failure_cards(fresh)
            break
    needed = pending(items, history, digest)
    changed = source_digest(workspace) != digest
    ok = not needed and not failures and not changed
    details = []
    for item in items:
        name = scenario_name(item)
        evidence = history["checks"].get(name, {})
        details.append(
            {
                "name": item["name"],
                "scenario_id": name,
                "source_ids": item["source_ids"],
                "consecutive_passes": evidence.get("consecutive_passes", 0)
                if evidence.get("source_digest") == digest
                else 0,
                "last_failure": evidence.get("last_failure"),
            }
        )
    report["stability"] = {
        "ok": ok,
        "required_consecutive_passes": REQUIRED_PASSES,
        "checks": details,
        "runs": runs,
        "failures": failures,
        "source_changed": changed,
    }
    report["checks"].append(
        {
            "step": "stability_confirmation",
            "ok": ok,
            "detail": (
                "Fresh independent copies confirmed consecutive passes"
                if ok
                else "Timing-sensitive evidence incomplete; see stability checks and reports"
            ),
            "repairable": any(card.get("repairable") for card in failures),
        }
    )
    report["ok"] = bool(report["ok"] and ok)
    write_json(output / "stability-report.json", report["stability"])
    return report
