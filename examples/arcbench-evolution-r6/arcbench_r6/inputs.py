"""Platform input preparation and requirement traceability."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from arc_light.spec import roots


def resolve_requirements_file(path: str) -> Path:
    candidate = Path(path).resolve()
    if candidate.is_dir():
        for name in ("requirements.yaml", "requirements.yml", "task.yaml"):
            file_path = candidate / name
            if file_path.is_file():
                return file_path
        raise FileNotFoundError(f"No requirements YAML found in {candidate}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Requirement path is not a file or directory: {candidate}")
    return candidate


def copy_template_contents_to_output(template_dir: Path, output_dir: Path) -> tuple[int, int]:
    """Fill in the agent's generic starter template under output_dir, without
    clobbering files the task workspace already ships (for example a
    task-specific database schema and the init code that wires it in). Only
    files missing from output_dir are written; everything already present
    (from the ARC-Bench task workspace) is left alone. Returns (written, skipped).
    """
    if not template_dir.is_dir():
        raise FileNotFoundError(f"Starter template directory not found: {template_dir}")
    written = 0
    skipped = 0
    for source in sorted((path for path in template_dir.rglob("*") if path.is_file())):
        relative = source.relative_to(template_dir)
        destination = output_dir / relative
        if destination.exists():
            skipped += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        written += 1
    return (written, skipped)


def read_task(requirement_file: Path) -> str:
    text = requirement_file.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Requirement file is empty: {requirement_file}")
    return text


def _persist_requirement_tree(runtime: Any, document: Any, *, resume: bool = False) -> list[str]:
    """Persist the input requirement tree without inventing requirement IDs."""
    runtime.traceability.init_db(reset=not resume)
    requirement_ids: list[str] = []

    def visit(node: dict[str, Any], parent_id: str | None = None) -> None:
        req_id = str(node.get("id") or node.get("req_id") or "").strip()
        if not req_id:
            return
        children = node.get("children") or node.get("requirements") or []
        if not isinstance(children, list):
            children = []
        requirement_ids.append(req_id)
        scenarios = node.get("scenarios")
        if not resume or runtime.traceability.get_requirement(req_id) is None:
            runtime.traceability.upsert_requirement(
                req_id=req_id,
                name=str(node.get("name") or node.get("title") or req_id),
                description=str(node.get("description") or node.get("text") or ""),
                visual_reference=node.get("visual_reference") or node.get("visual_references"),
                scenarios=scenarios if isinstance(scenarios, list) else [],
                parent_id=parent_id,
                children_ids=[
                    str(child.get("id") or child.get("req_id") or "").strip()
                    for child in children
                    if isinstance(child, dict) and str(child.get("id") or child.get("req_id") or "").strip()
                ],
                dependencies=node.get("dependencies") if isinstance(node.get("dependencies"), list) else [],
            )
        for child in children:
            if isinstance(child, dict):
                visit(child, req_id)

    for node in roots(document):
        visit(node)
    return list(dict.fromkeys(requirement_ids))
