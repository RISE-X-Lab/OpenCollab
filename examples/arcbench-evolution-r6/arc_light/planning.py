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


def _components(nodes, edges):
    """Tarjan SCC: a dependency cycle is scheduled together instead of silently ignored."""
    indices, low, stack, active, result = {}, {}, [], set(), []

    def visit(node):
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for dependency in sorted(edges[node]):
            if dependency not in indices:
                visit(dependency)
                low[node] = min(low[node], low[dependency])
            elif dependency in active:
                low[node] = min(low[node], indices[dependency])
        if low[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                active.remove(member)
                component.append(member)
                if member == node:
                    break
            result.append(component)

    for node in nodes:
        if node not in indices:
            visit(node)
    return result


def feature_groups(index):
    rows = target_rows(index)
    by_id = {row["id"]: row for row in index}
    focused = {row["id"] for row in rows}
    dependencies = {}

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
    for row in rows:
        dependencies[row["id"]] = focus_dependencies(row["id"], set()) - {row["id"]}
        buckets.setdefault(resource_key(row), []).append(row)
    units = list(buckets.values())
    owners = {row["id"]: n for n, unit in enumerate(units) for row in unit}
    edges = {
        n: {owners[dep] for row in unit for dep in dependencies[row["id"]] if owners[dep] != n}
        for n, unit in enumerate(units)
    }
    components = _components(range(len(units)), edges)
    position = {row["id"]: n for n, row in enumerate(rows)}
    groups = []
    for component in components:
        grouped = sorted((row for n in component for row in units[n]), key=lambda r: position[r["id"]])
        ids = {row["id"] for row in grouped}
        groups.append(
            {
                "ids": [row["id"] for row in grouped],
                "rows": grouped,
                "resources": sorted({resource_key(row) for row in grouped}),
                "dependencies": sorted({dep for row in grouped for dep in dependencies[row["id"]]} - ids),
                "dependency_cycle_merged": len(component) > 1,
            }
        )
    pending, ordered = list(groups), []
    while pending:
        done = {key for group in ordered for key in group["ids"]}
        ready = [group for group in pending if set(group["dependencies"]) <= done]
        group = min(ready, key=lambda g: min(position[key] for key in g["ids"]))
        pending.remove(group)
        ordered.append(group)
    return ordered


def allocation(remaining, weights, *, minimum=200_000):
    """Fair weighted share, preserving a floor for every later group."""
    if not weights or remaining < minimum:
        return 0
    floor = min(minimum, remaining // len(weights))
    share = int(remaining * weights[0] / sum(weights))
    return min(remaining - floor * (len(weights) - 1), max(minimum, share))


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


def affected_requirements(group, completed, changed_files):
    """Recheck prior groups sharing edited files/resources or a dependency edge."""
    affected = set(group["ids"])
    broad = any(
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
    changed = set(changed_files)
    while True:
        before = set(affected)
        for prior in completed:
            if (
                broad
                or changed.intersection(prior["changed_files"])
                or set(group["resources"]).intersection(prior["resources"])
                or set(prior["dependencies"]).intersection(affected)
                or set(group["dependencies"]).intersection(prior["ids"])
            ):
                affected.update(prior["ids"])
        if affected == before:
            return sorted(affected)


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
