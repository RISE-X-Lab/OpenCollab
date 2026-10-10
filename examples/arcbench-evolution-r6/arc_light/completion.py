"""Conservative completion decisions; model prose is never test evidence."""

import re

from .spec import unfinished_ids


def completion_targets(result, index, ledger=None):
    reason = str(result.get("reason") or "").lower()
    if result.get("status") != "completed" and any(
        word in reason for word in ("budget", "step limit", "timeout", "timed out")
    ):
        return [], "main limit reached; use concrete acceptance failures for bounded repairs"
    explicit = unfinished_ids(result.get("final_summary", ""), index)
    if ledger:
        # Unknown progress remains work, but lack of verification is not a request to rewrite.
        implementation = [
            key for key, row in ledger.items() if row.get("state") in {"pending", "in_progress", "failed"}
        ]
        if explicit:
            implementation = [key for key in implementation if key in explicit]
        if any(row.get("state") != "pending" for row in ledger.values()):
            return implementation, "checkpoint implementation work only; verification goes to acceptance"
    summary = result.get("final_summary", "")
    declared = re.search(r"^Unfinished:\s*none\b", summary, re.I | re.M)
    if explicit:
        return explicit, "explicit unfinished requirements"
    if result.get("status") == "completed" and declared:
        return [], "explicit completion claim; acceptance still required"
    focus = [row["id"] for row in index if row["atomic"] and row.get("evolution_change")]
    return focus or [row["id"] for row in index if row["atomic"]], "interrupted or missing usable completion summary"


def scenario_key(item):
    """Use the original requirement ID and scenario ordinal as the domain identity."""
    return f"{item['requirement_id']}:scenario_{item['scenario_index'] + 1}"


def scenario_coverage(expected, checks, *, requirements_without_scenarios=()):
    """Account for every requested public scenario, including unmapped adapters."""
    rows = []
    for item in expected:
        key = scenario_key(item)
        name = f"evolution:{item['name']}:scenario_{item['scenario_index'] + 1}"
        matches = [
            check
            for check in checks
            if check.get("requirement_id") == item["requirement_id"]
            and check.get("scenario_index") == item["scenario_index"]
        ]
        mapped = item.get("mapped", True)
        state = matches[0].get("status", "unverified") if len(matches) == 1 and mapped else "unverified"
        reason = (
            "unmapped_requirement"
            if not mapped
            else "duplicate_results"
            if len(matches) > 1
            else "not_executed"
            if not matches
            else None
        )
        rows.append(
            {
                "scenario_id": key,
                "name": name,
                "requirement_id": item["requirement_id"],
                "scenario_index": item["scenario_index"],
                "source_ids": item["source_ids"],
                "status": state,
                "reason": reason,
                "blocked_by": matches[0].get("blocked_by") if len(matches) == 1 else None,
            }
        )
    counts = {
        state: sum(row["status"] == state for row in rows)
        for state in ("passed", "failed", "blocked", "skipped", "unverified")
    }
    missing = list(requirements_without_scenarios)
    return {
        "expected": len(rows),
        **counts,
        "mapped": sum(item.get("mapped", True) for item in expected),
        "requirements_without_scenarios": missing,
        "complete": bool(rows) and not missing and counts["passed"] == len(rows),
        "scenarios": rows,
    }
