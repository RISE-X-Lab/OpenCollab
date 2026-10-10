"""Dependency/resource-aware scheduling; execution records never certify acceptance."""

from __future__ import annotations

import hashlib
import json


def target_rows(index):
    leaves = [row for row in index if row.get("atomic")]
    return [row for row in leaves if row.get("evolution_change")] or leaves


def resource_key(row):
    """Small, inspectable hints about shared implementation, not business/test behavior."""
    name = row.get("name", "").lower()
    if any(
        word in name
        for word in (
            "cell",
            "worksheet",
            "workbook",
            "freeze",
            "named range",
            "filter rows",
            "numeric validation",
            "conditional formatting",
        )
    ):
        if "rename" in name:
            return "sheet_identity"
        if any(word in name for word in ("filter", "validation")):
            return "sheet_constraints"
        if any(word in name for word in ("formatting", "note")):
            return "sheet_annotations"
        return "sheet_document_state"
    if any(word in name for word in ("sign in", "register", "browser sessions")):
        return "account_session"
    if "organization" in name:
        return "organization"
    if ("search" in name and "repositor" in name) or any(word in name for word in ("archive", "branches")):
        return "repository_navigation"
    # Keep unrelated unknown features focused; explicit dependencies still order them.
    return "feature:" + row["id"]


def feature_groups(index):
    from opencollab.builtin_workflows.evolution import EvolutionGroup, plan_evolution_groups

    rows = target_rows(index)
    if not rows:
        return []
    by_id = {row["id"]: row for row in index}
    focused = {row["id"] for row in rows}

    def focus_dependencies(key, seen):
        if key in seen:
            return set()
        seen = seen | {key}
        found = set()
        for dependency in by_id.get(key, {}).get("dependencies", []):
            if dependency in focused:
                found.add(dependency)
            else:
                found.update(focus_dependencies(dependency, seen))
        return found

    buckets = {}
    dependencies = {}
    for row in rows:
        dependencies[row["id"]] = focus_dependencies(row["id"], set()) - {row["id"]}
        buckets.setdefault(resource_key(row), []).append(row)
    groups = []
    for number, (resource, unit) in enumerate(buckets.items(), 1):
        identifier = f"arc-resource-{number}"
        while identifier in focused:
            identifier = "_" + identifier
        groups.append(EvolutionGroup(
            id=identifier,
            prompt="Implement the current requirement group.",
            weight=sum(max(1, row.get("scenario_count", 0)) for row in unit),
            targets=tuple(row["id"] for row in unit),
            dependencies=tuple(sorted({key for row in unit for key in dependencies[row["id"]]}
                                      - {row["id"] for row in unit})),
            resources=(resource,),
        ))
    owners = {row["id"]: number for number, unit in enumerate(buckets.values()) for row in unit}
    result = []
    for group in plan_evolution_groups(groups):
        selected = set(group.targets)
        grouped = [row for row in rows if row["id"] in selected]
        result.append(
            {
                "ids": [row["id"] for row in grouped],
                "rows": grouped,
                "resources": sorted(group.resources),
                "dependencies": sorted(group.dependencies),
                "dependency_cycle_merged": len({owners[key] for key in group.targets}) > 1,
            }
        )
    return result


def group_weight(group):
    return sum(max(1, row.get("scenario_count", 0)) for row in group["rows"])


def source_inventory(workspace):
    result = {}
    for name in ("frontend", "backend"):
        root = workspace / name
        paths = list(root.glob("*")) + list((root / "src").rglob("*"))
        for path in sorted(paths):
            if path.is_file() and path.suffix in {
                ".js",
                ".jsx",
                ".ts",
                ".tsx",
                ".json",
                ".css",
                ".html",
                ".sql",
                ".py",
            }:
                result[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def affects_prior_groups(changed_files):
    """Shared application paths require checks for all prior requirement groups."""
    return any(
        path.endswith(("package.json", "package-lock.json"))
        or any(
            part in path.lower()
            for part in (
                "/database/",
                "/hooks/",
                "/store/",
                "/stores/",
                "/context/",
                "/contexts/",
                "/lib/",
                "/api.",
                "/app.",
                "/layout.",
                "/index.",
            )
        )
        for path in changed_files
    )


def group_prompt(group, progress, directory=".arc/evolution-spec/by-id"):
    listing = "\n".join(
        f"- {r['id']} | {r.get('name', r['id'])} | {directory}/{r['file']} | {r.get('scenario_count', 0)} scenarios"
        for r in group["rows"]
    )
    return f"""Implement this bounded group in the EXISTING application:
{listing}
Shared implementation resources: {", ".join(group["resources"])}.
Prior-group dependencies: {", ".join(group["dependencies"]) or "(inherited or none)"}.
Read the overview, these cards and their distinct ancestors; cover ALL their scenarios.
Other cards are preserved contracts, not additional implementation scope. Locate inherited
dependencies before changing them. Inspect the current UI, API response and persistence path.
Keep shared state/serialization, formula evaluation and ordinary edits in one consistent path.
Start from each scenario's GIVEN page and identity; direct URLs do not verify a missing entry.
Use checkpoint for implementation details and run_acceptance for concrete risks. The scheduler
checks this group and affected earlier groups after you return, then runs full acceptance.
Do not create a replacement UI suite or repeat unchanged checks. Run manual mutation tests only
on disposable application/database copies; never reset or clean up the working database.
If the group reaches its allocation, retain existing changes and report remaining work; later
groups have reserved execution opportunities. Failed checks feed the final bounded repair loop.
Prior execution, claims and coordinator evidence (claims are not acceptance):
{progress.handoff()}
Finish with concise changed paths and unfinished IDs. A missing/malformed summary neither
certifies success nor triggers a repeat implementation pass.
"""


def plan_summary(groups):
    return json.loads(json.dumps([{key: value for key, value in group.items() if key != "rows"} for group in groups]))
