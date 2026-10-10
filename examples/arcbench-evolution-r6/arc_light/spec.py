"""One copy of each original requirement; compact metadata only."""

import re

import yaml

# Published additive features have no "Modified Feature Description" marker.
# Names select requirement cards, never business behavior or seed values.
EVOLUTION_ADDITIONS = frozenset(
    {
        "Manage Active Browser Sessions",
        "View Organization Audit Log",
        "Archive and Restore a Repository",
        "Create and View Repository Releases",
        "Add and Remove Issue Reactions",
        "Freeze Rows and Columns",
        "Find and Replace Cell Text",
        "Create and Edit Named Ranges",
        "Create, Edit, and Delete Conditional Formatting Rules",
        "Create, Edit, and Delete a Cell Note",
    }
)


def roots(document):
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if not isinstance(document, dict):
        return []
    if document.get("id") or document.get("req_id"):
        return [document]
    for key in ("requirements", "requirement_tree", "nodes", "children"):
        value = document.get(key)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def write_cards(document, output, *, directory_name="requirements/by-id"):
    directory = output / directory_name
    root_id = str(document.get("id") or document.get("req_id") or "") if isinstance(document, dict) else ""
    rows, cards, seen = [], {}, set()

    def visit(node, ancestors, inherited_dependencies=()):
        req_id = str(node.get("id") or node.get("req_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", req_id) or req_id in seen:
            raise ValueError(f"Invalid or duplicate requirement id: {req_id!r}")
        seen.add(req_id)
        children = node.get("children") or node.get("requirements") or []
        filename = "_overview.yaml" if req_id == root_id and not ancestors else req_id + ".yaml"
        card = {key: value for key, value in node.items() if key not in ("children", "requirements")}
        card["parent_id"] = ancestors[-1] if ancestors else None
        card["children_ids"] = [str(child.get("id") or child.get("req_id") or "") for child in children]
        card["ancestor_cards"] = [("_overview.yaml" if owner == root_id else owner + ".yaml") for owner in ancestors]
        cards[filename] = card
        declared = node.get("dependencies") or []
        declared = declared if isinstance(declared, list) else [declared]
        dependencies = list(
            dict.fromkeys(
                [
                    *inherited_dependencies,
                    *[
                        str(value.get("id") or value.get("req_id") or "") if isinstance(value, dict) else str(value)
                        for value in declared
                    ],
                ]
            )
        )
        dependencies = [value for value in dependencies if value and value != req_id]
        rows.append(
            {
                "id": req_id,
                "name": str(node.get("name") or node.get("title") or req_id),
                "parent_id": card["parent_id"],
                "atomic": not children,
                "file": filename,
                "dependencies": dependencies,
                "evolution_change": "modified"
                if "Modified Feature Description" in str(node.get("description", ""))
                else "added"
                if node.get("name") in EVOLUTION_ADDITIONS
                else None,
                "scenario_count": len(node.get("scenarios") or []),
            }
        )
        for child in children:
            visit(child, [*ancestors, req_id], dependencies)

    for node in roots(document):
        visit(node, [])
    if "_overview.yaml" not in cards:
        if isinstance(document, dict):
            cards["_overview.yaml"] = {
                key: value
                for key, value in document.items()
                if key not in ("children", "requirements", "requirement_tree", "nodes")
            }
        else:
            cards["_overview.yaml"] = {"note": "Top-level requirement cards contain the product contracts."}
    directory.mkdir(parents=True, exist_ok=True)
    for filename, card in cards.items():
        (directory / filename).write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return rows


def unfinished_ids(summary, index):
    known = {row["id"] for row in index if row["atomic"]}
    match = re.search(r"^Unfinished:[ \t]*(.*)$", summary or "", re.I | re.M)
    if not match:
        return []
    text = match.group(1)
    # "none of the IDs (REQ-...) is unfinished" is not a list of missing IDs.
    if re.match(r"none\b", text, re.I) and not re.match(r"none\s+except\b", text, re.I):
        return []
    return [
        row["id"]
        for row in index
        if row["id"] in known and re.search(r"(?<![\w-])" + re.escape(row["id"]) + r"(?![\w-])", text)
    ]


def report_summary(output):
    """Keep the handoff fields before shortening prose or file lists."""
    lines = re.findall(r"^(?:Unfinished|Blockers|Files):[^\r\n]*", str(output or ""), re.I | re.M)
    return (
        "\n".join(line if line.lower().startswith("unfinished:") else line[:600] for line in lines)
        if lines
        else str(output or "")[-1800:]
    )


def completion_prompt(index, missing, summary, interrupted, target_ids=None):
    by_id = {row["id"]: row for row in index}
    ids = set(target_ids) if target_ids is not None else set(unfinished_ids(summary, index))
    if not ids:
        ids = {row["id"] for row in index if row["atomic"] and row.get("evolution_change")}
        if not ids:
            ids = {row["id"] for row in index if row["atomic"]}
    files = {by_id[req_id]["file"] for req_id in ids}
    hints = [item for item in missing if ids.intersection(item["ids"])][:8]
    return f"""Continue the existing implementation in this workspace. Do not restart the app design.
Main pass summary (self-report, not acceptance evidence):
{summary or "(not supplied)"}
Main stopped/failed: {interrupted}.
Target cards: {", ".join(sorted(files)) or "(recover the interrupted operation from existing changes)"}.
Missing source-text hints (not functional coverage; ignore examples/prose/runtime values):
{repr(hints)}
Fix only the unfinished operations above; hints do not create additional targets.
For interruption recovery without a usable summary, inspect existing changes and use the
original requirement index to finish remaining paths; absence of a summary is not completion.
Read only relevant cards and source. Read each distinct ancestor once while its contents remain
in context; ancestor_cards in the leaf points to it. After compaction, reread only if needed.
Fix shared navigation/auth/seed blockers first, then implement the missing operations through
their visible controls and persist successful writes. Check failure and reload behaviour too.
Use the supplied Git baseline and rg to locate relevant source instead of reading all source.
Do not write a new test suite just to produce a coverage report. Run focused checks when useful.
Use run_acceptance for the affected requirement IDs after editing. The coordinator performs
the final complete check; do not create a substitute suite or repeat it in a shell script.
Use checkpoint to update each affected requirement's state, files, evidence and next action.
Implemented-but-untested paths go to independent acceptance; do not rewrite them simply because
an environment capability is missing. Read .arc/checks/preflight.json for actual environment errors.
Finish with three brief lines: Unfinished: original IDs or none; Blockers: remaining blockers;
Files: key implementation paths. No claim of official test passes.
"""


def strip_code_comments(text: str) -> str:
    """Small lexer keeps quoted URLs and literals while excluding JS/CSS comments from hints."""
    result: list[str] = []
    position = 0
    quote = ""
    while position < len(text):
        char = text[position]
        pair = text[position : position + 2]
        if quote:
            result.append(char)
            if char == "\\" and position + 1 < len(text):
                position += 1
                result.append(text[position])
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
            result.append(char)
        elif pair == "//":
            end = text.find("\n", position + 2)
            position = len(text) if end < 0 else end
            result.append("\n")
            continue
        elif pair == "/*":
            end = text.find("*/", position + 2)
            position = len(text) if end < 0 else end + 2
            result.append(" ")
            continue
        else:
            result.append(char)
        position += 1
    return "".join(result)
